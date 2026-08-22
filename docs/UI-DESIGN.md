# UI Design System — Soccer-Cam

Soccer-Cam and Team Tech Tools are one product family with **one design system**.
The tokens in `video_grouper/web/static/soccer-cam.css` hold the same values as
TTT's `frontend/shared/styles/theme.css` (dark theme). Colour, type, radius,
spacing and motion are shared verbatim.

The two differ in **register**, not palette:

| | Team Tech Tools | Soccer-Cam |
|---|---|---|
| What it is | A workspace you think in | An appliance console you watch |
| Themes | Light + dark | Dark only (`color-scheme: dark`) |
| Density | Roomy, prose and tables | Dense, mono-forward, status-led |
| Extra vocabulary | — | The tally: capture state, everywhere |

Litmus test, inherited from TTT: *"Would this feel at home on an ESPN production
desk?"* If it feels like a startup landing page or a Material template, strip it
back.

---

## Colour has three separate jobs

This is the rule the whole system hangs on. Keep the three apart.

| Token | Means | Told apart by |
|---|---|---|
| `--color-accent` (blue) | Interactive: links, tabs, focus | — |
| `--color-record` (red) | Capture in progress | **Motion** — the only pulsing thing |
| `--color-danger` (red) | Failure | Static, tinted background, icon |
| `--color-warning` (amber) | A warning, and nothing else | — |

Record and danger share a hue deliberately — red tally is the broadcast
convention — and are separated by motion and form, never by hue alone. That is
also the accessible choice: colour is never the sole carrier of meaning.

**Amber is not an accent.** Soccer-Cam used to paint its accent
`#fb923c` while `--signal-warn` sat at `#fbbf24` — the same colour doing two
jobs, so nothing amber meant anything in particular. Amber now means warning.

### Palette

Dark only. Values match TTT's dark theme exactly.

```
--color-bg-primary    #0f0f12     --color-text-primary    #f0f0f2
--color-bg-secondary  #15151a     --color-text-secondary  rgba(240,240,242,.65)
--color-bg-card       #1a1a20     --color-text-muted      rgba(240,240,242,.38)
--color-bg-tertiary   #0a0a0e     --color-border          #2a2a32
--color-accent        #5b8def     --color-record          #ff3b30
--color-success       #4ade80     --color-warning         #f59e0b
--color-danger        #ef4444     --color-idle            #6b7280
```

Never hardcode a colour in page CSS. The one exception is **data-encoding
colour**: canvas strokes and categorical label swatches in the annotation tools,
where each class must stay mutually distinguishable. Those are not brand colours
and are exempt — but they must not be reused for chrome.

---

## Type

| Role | Token | Face | Used for |
|---|---|---|---|
| Display | `--font-headline` | Barlow Condensed 600/700 | Page and section titles, brand |
| Body | `--font-body` | Inter 400/500/600 | Everything you read |
| Mono | `--font-mono` | JetBrains Mono 400/500/600 | Timecodes, paths, IDs, eyebrows |

Same three faces as TTT. Barlow Condensed is the handshake between the two
products — you can tell at a glance that they belong together.

The fallback chains are load-bearing, not decoration: the seam-calibration tool
runs on a phone at a pitch and **deliberately skips the webfont request**, so
`Bahnschrift` / `DIN Alternate` / `Arial Narrow` have to carry the condensed
look on their own. Do not trim the fallbacks.

Mono is also the eyebrow face: 11px, `letter-spacing: 0.2em`, uppercase.

---

## Structure

### Navigation: two places, and tasks

The topbar has exactly **two** destinations, because there are only two ongoing
modes for an appliance:

- **Status** (`/`) — what it is doing right now
- **Settings** (`/config`) — everything you can change

Everything else is a **task**, reached from the context it acts on, and marked
with a crumb rather than a nav highlight:

- **`/setup/*`** — onboarding. Entered automatically while the config is
  incomplete; offered from Settings as "Run setup again". As a nav peer of
  Settings it would be a second door to the same fields with nothing to say
  which door to use.
- **`/stitch`** — seam calibration. A job you do after a knock or a remount, on
  a phone, so it launches from the camera it belongs to.

Long pages get an in-page rail (`.shell--rail`) listing their sections.

### Numbering means sequence

Use ordinal markers **only where order is real**. The setup wizard is a genuine
sequence and gets a step tracker. The settings page is a flat set of INI
sections — it used to carry `§ 01 … § 16`, which implied a procedure that does
not exist. It doesn't any more. Don't put it back.

### The tally — the signature element

A 3px strip pinned to the top of every page, driven by real capture state:

```html
<div class="tally" data-state="idle|armed|recording|error"></div>
```

- `idle` — a plain hairline. **Asserts nothing.** Pages that cannot see the
  pipeline render this, so the strip never claims a state it doesn't know.
- `armed` — cameras connected, nothing in flight
- `recording` — work in flight; live red, slow pulse
- `error` — a camera is configured but not connected

It sits above the sticky topbar (`--z-toast`). A capture indicator a header can
cover is not an indicator.

Nothing else in the product animates on a loop.

---

## Rules

1. **One stylesheet.** `video_grouper/web/static/soccer-cam.css`, served at
   `/static/soccer-cam.css` by the orchestrator and `/shared/soccer-cam.css` by
   the annotation server. One file, two servers, never a copy.
2. **Build pages from `chrome.py`.** `head()`, `topbar()`, `tally()`, `page()`,
   `notice_page()`. Every page that rolls its own `<head>` is how the product
   drifted into four background colours and three font stacks in the first
   place.
3. **Page CSS is for page-specific layout only** — a canvas, a dock, a settings
   grid. If two pages need it, it belongs in the shared sheet.
4. **Never restyle a shared class in page CSS.** If `.btn` needs a variant, add
   the variant to the shared sheet. Page-local names get a prefix
   (`.cfg-field`, not `.field`).
5. **Token values only.** Radius `4 / 6 / 12px` (2px for tags, 50% for circles).
   Spacing in multiples of 4. Font sizes from the scale. `px`, not `rem`.
6. **Never `transition: all`** — name the properties.
7. **No pill radii.** Nothing gets `border-radius: 999px`.
8. **Respect `prefers-reduced-motion`**, and keep the tally legible without it.
9. **Field pages skip the CDN.** `webfonts=False, inline_css=True` — no
   follow-up request on a phone at a pitch.

## Writing

- Sentence case. Plain verbs. No shouting — `CONFIG WRITTEN — RELOAD ON NEXT
  SERVICE TICK` is now "Configuration saved. The service picks it up on its next
  tick."
- An action keeps its name through the flow: the button says *Save changes*, so
  the banner says *Configuration saved*.
- Name things by what people control: *Settings*, not *Configuration*; *Status*,
  not *Dashboard*. Use the same word everywhere, including the `<title>`.
- Empty states invite an action. "No cameras configured yet" ships with an
  *Add a camera* button.
- Errors say what happened and what to do. They don't apologise.
