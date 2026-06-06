# App UI — screen-by-screen spec

SwiftUI app with a small surface: 5 top-level screens. Branding per
[[feedback_custom_branding_from_start]] — custom icon + splash designed
before the first build, not after.

## Tab structure

The app's root is a single-pane navigation stack (NOT a tab bar — the
information density doesn't justify tabs in v1).

```
NavigationStack
└── GamesListView           (root)
    ├── GameDetailView      (push)
    ├── ImportFlowView      (sheet)
    ├── SignInView          (sheet)
    └── SettingsView        (sheet, from toolbar)
```

## 1. GamesListView (root)

The home. Shows every Game the user has, grouped into sections.

```
┌──────────────────────────────────┐
│ ≡ Soccer-Cam            ⚙️ Add ▾ │  ← toolbar: Settings (left), Add menu (right)
├──────────────────────────────────┤
│ Today                            │
│ ┌────────────────────────────┐   │
│ │ 🎥 Flash vs Heat           │   │  ← live: pulsing dot, progress strip
│ │    10:00 AM · Live         │   │
│ │    ▓▓▓▓▓▓▓▓▓▓░░░░  6/12    │   │
│ └────────────────────────────┘   │
│                                  │
│ This week                        │
│ ┌────────────────────────────┐   │
│ │ 📼 Flash vs Cobras          │  │  ← rendered: shows thumbnail
│ │    Sat 9:00 AM · Uploaded   │  │
│ └────────────────────────────┘   │
│ ┌────────────────────────────┐   │
│ │ ⚠️ Flash vs Bulldogs        │  │  ← failed: red icon, "Tap to retry"
│ │    Wed 7:00 PM · Failed     │  │
│ └────────────────────────────┘   │
└──────────────────────────────────┘
```

**Empty state (per [[feedback_no_nonsensical_demo_data]]):**

```
┌──────────────────────────────────┐
│ ≡ Soccer-Cam               Add ▾│
├──────────────────────────────────┤
│                                  │
│         📷                       │
│   No games yet                   │
│                                  │
│   Add a recording from your      │
│   Reolink camera, or import a    │
│   recorded panorama file.        │
│                                  │
│      [ + Connect camera ]        │
│      [ + Import file ]           │
│                                  │
└──────────────────────────────────┘
```

No fake "demo game" placeholder — the empty state explains what the app
does and offers the two real entry points.

**Add menu:**

- "Connect Reolink camera" → `ImportFlowView` (Reolink setup wizard)
- "Import recorded file" → System Files picker → `ImportFlowView` (bulk
  import path)

## 2. GameDetailView

Per-game state, per-segment progress strip, and the rendered output once
ready.

```
┌──────────────────────────────────┐
│ ◀ Games   Flash vs Heat   ⋯     │
├──────────────────────────────────┤
│                                  │
│   [ Rendered video preview ]     │  ← AVPlayer once final.mp4 ready;
│   ▶ ━━━━━━━━━━━━━━━━━━━━━━ 00:00 │    "rendering..." placeholder until then
│                                  │
├──────────────────────────────────┤
│ Status: Rendering (8/12 done)    │
│ Storage: 412 MB used             │
│                                  │
│ Segments                         │
│ ┌────────────────────────────┐   │
│ │ ▰▰▰▰▰▰▰▰▱▱▱▱  8 / 12       │  │  ← segment status strip, 1 cell per segment
│ │ ✅ ✅ ✅ ✅ ✅ ✅ ✅ ✅ 🔄 ⏳ ⏳ ⏳ │  │  ← per-segment icons (done / processing / pending)
│ └────────────────────────────┘   │
│                                  │
│ Camera                           │
│ Reolink @ 192.168.1.42           │
│ Last segment: 4 min ago          │
│                                  │
│ Detection                        │
│ Community ball detector v3       │
│                                  │
│ [ Mark game complete ]           │  ← only when status==downloading
│ [ Share rendered video ]         │  ← only when status==complete/uploaded
│                                  │
└──────────────────────────────────┘
```

**Per-segment tap:** drills into a tiny `SegmentDetailView` with timing
breakdown, error if any, retry button if failed. No video preview at the
segment level (only the full game gets preview).

**Upcoming-state handling per [[feedback_no_nonsensical_demo_data]]:**

A scheduled / future game shows "Game hasn't started yet" with no fake
empty timeline — just a "Cancel" and "Mark started early" button.

## 3. ImportFlowView (sheet)

Two flows, picked by Add menu:

### 3a. Connect Reolink camera

Step-through wizard per [[project_wizard_ux]] — OAuth login + auto-advance
pattern applied to camera-credential entry too:

1. Welcome + "Connect to camera" — explains the field-Wi-Fi prerequisite
2. Camera IP + username — text fields with "Test connection" button
3. Camera password — secure field, persisted to Keychain
4. Connection test result — auto-advances on success
5. Game metadata: display name, model picker (community / TTT-free / TTT-premium)
   - Premium models are visible but require sign-in to select; tap one →
     "Sign in to TTT to use premium models" → SignInView sheet
6. Confirm + start polling

### 3b. Import recorded file

1. System `UIDocumentPicker` (or `PHPickerViewController` for Photos)
2. Show file info: duration, resolution, codec
3. Game metadata (same as Reolink flow)
4. Confirm + start virtual-segment processing

Either flow ends with a push to `GameDetailView` for the newly-created
Game.

## 4. SignInView (sheet)

OAuth via Supabase, presented when:

- User taps "Sign in" from Settings
- User selects a premium model that requires entitlement
- User taps "Upload to TTT" on a rendered game and no session exists

Apple HIG-compliant: dismissible, no force-skip-impossible nag. If the
user dismisses without signing in, the calling action is cancelled (the
premium model is unselected; the upload is paused).

Sign-in itself opens `ASWebAuthenticationSession`. Per
[[project_wizard_ux]] the post-callback experience auto-advances to "what
this unlocked" — typically a one-screen "You're signed in as
mark@example.com" that auto-dismisses after 2s.

## 5. SettingsView (sheet)

- Sign in / out
- Current TTT account
- Model preferences (default model for new games)
- Storage management ("Delete N rendered videos" — never deletes raw if
  game is still live)
- About / open-source licenses
- Diagnostic logs (debug builds only)
- Environment picker (debug builds only, per [[feedback_preview_before_prod]])

No "feedback" or "rate the app" pestering — the OSS posture makes
GitHub Issues the right feedback channel.

## Visual design

- System (light + dark) — no custom themes in v1.
- Accent color: a green that reads as "go / live" but distinct from the
  iOS default. Picked in Assets.xcassets, applied via
  `.tint(.accentColor)`.
- Custom SF Symbols equivalent for the soccer ball — `BallIcon.swift`
  drawing path, used for app icon + tab placeholder.
- Custom app icon — pre-launch deliverable per
  [[feedback_custom_branding_from_start]]; rough direction: a small
  panorama / cylindrical shape over a green field, in 2-tone style that
  matches iOS 18 icon guidance.
- Custom splash — same iconography on a solid accent background; loads
  as a `LaunchScreen.storyboard`.

## Accessibility

- VoiceOver labels on every interactive element. Segment status icons
  read as "Segment 8 of 12, rendered" not just the emoji.
- Dynamic Type — all text uses semantic font styles (`.body`, `.headline`,
  etc.) so it scales.
- Reduce Motion — the pulsing live-dot pause when reduce-motion is on.
- High-contrast color palette — accent color picked for >4.5:1 contrast
  on both light and dark.

## Cross-references

- `data_model.md` — what the UI displays
- `architecture.md#concurrency-model` — how the UI observes state
- `ttt_api_integration.md#auth-flow` — what SignInView delegates to
- `app_store_plan.md` — privacy nutrition labels, the screenshots strategy
