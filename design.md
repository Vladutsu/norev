# norev UI design

The design language for the norev desktop app (pywebview). It is soft neumorphism on a calm,
Apple-style structure: neumorphism gives the surfaces their feel, Apple's Human Interface
Guidelines decide layout, hierarchy, type, motion and accessibility.

Reference mock: the "norev Studio — neumorphism" canvas (screens: New Session, Recording, To Review).
Terms follow `docs/GLOSSARY.md`.

## 1. Principles

| Principle | Source | In norev |
|---|---|---|
| Clarity | Apple | One primary action per screen. Text is legible at every size; icons always have a label or `aria-label`. |
| Deference | Apple | The UI steps back during a Session: the Live Transcript is the content, chrome stays quiet and neutral. |
| Depth | Apple + neumorphism | Hierarchy comes from raised vs inset surfaces, not from borders or colored boxes. |
| Soft tactility | Neumorphism | Controls look pressable (raised); places that hold content or input look carved in (inset). |
| Calm | Both | Light ground, few colors, no gradient washes, no busy decoration. A doctor uses it during a consultation. |
| Trust is visible | norev (ADR 0002, 0004) | "Processed locally" is always shown; the approval screen shows the exact payload before anything leaves. |

## 2. Where neumorphism is used, and where it is not

Neumorphism has known accessibility problems (low contrast, unclear states). Rules:

- **Use it for surfaces:** the app shell, panels, cards, inputs, segmented controls, round buttons.
- **Never let a shadow be the only signal.** A selected, pressed or disabled state also changes text
  color or weight, an icon, or a label.
- **The primary action is solid accent**, not a neumorphic bump: "Start Session", "Stop and analyse",
  "Approve and send". Neutral and secondary actions are neumorphic.
- **Text never sits on a low-contrast surface:** every text color below passes 4.5:1 on the ground.
- **Focus is a 2 px accent outline**, offset 2 px, never only a shadow change.
- **No nested raised-in-raised more than two levels.** Raised panel → inset well → raised chip is the
  deepest stack.

## 3. Tokens

### Color (light theme, as in the mock)

| Token | Value | Use |
|---|---|---|
| `bg` | `#E7ECF3` | ground and every neumorphic surface |
| `shadow-dark` | `#C5CEDB` | raised, bottom-right |
| `shadow-light` | `#FFFFFF` | raised, top-left |
| `inset-dark` | `#C9D2DF` | inset, top-left |
| `text` | `#1E2633` | primary text |
| `text-2` | `#3A4456` | secondary text, icons |
| `text-3` | `#4A5466` | captions, labels, overlines |
| `text-disabled` | `#5B6576` | disabled labels (still ≥4.5:1) |
| `accent` | `#2477F2` | primary buttons, waveform, progress, focus ring |
| `accent-text` | `#1F63D6` | accent used as text or icon on `bg` |
| `success` | `#13A26B` (dot) / `#0E7A50` (text) | local-processing dot, "Approved" |
| `recording` | `#D93A3A` | recording dot, always with the word "Recording" |
| `attention` | `#E0702B` (ring) / `#B4540F` (text) | required field still empty, the Patient's Speaker color |

Speaker colors must differ in lightness as well as hue (blue `#1F63D6` vs orange `#B4540F`), never
red/green.

### Color (dark theme, to design next)

Same rules, same structure: ground `#1E232B`, shadow-dark `#14181E`, shadow-light `#2A313B`,
text `#E8EDF4`, text-2 `#B7C0CD`, accent unchanged. Check every pair for 4.5:1 before shipping.

### Elevation

| Token | CSS |
|---|---|
| `raised` | `box-shadow: 9px 9px 20px #C5CEDB, -9px -9px 20px #FFFFFF` |
| `raised-sm` | `box-shadow: 5px 5px 12px #C5CEDB, -5px -5px 12px #FFFFFF` |
| `inset` | `box-shadow: inset 5px 5px 11px #C9D2DF, inset -5px -5px 11px #FFFFFF` |
| `accent-lift` | `box-shadow: 0 10px 24px rgba(36,119,242,.35), inset 2px 2px 6px rgba(255,255,255,.35)` |

Light always comes from the top-left.

### Typography

Apple uses SF Pro; we use **Inter** (body) and **Manrope** (display), both open, both close to SF
in feel. Monospace for payloads: **JetBrains Mono**.

| Style | Font | Size / line | Weight |
|---|---|---|---|
| Large title | Manrope | 46 / 1.1, tracking −1px | 800 |
| Title | Manrope | 40 / 1.1 | 800 |
| Headline | Manrope | 18–20 | 700 |
| Body | Inter | 15–16 / 1.6 | 400–500 |
| Callout / label | Inter | 13–14 | 500–600 |
| Overline | Inter | 12, tracking 2px, uppercase | 600 |
| Payload | JetBrains Mono | 13 / 1.6 | 400 |

Text sizes scale with the OS setting (Apple's Dynamic Type idea): use `rem`, never fixed px in the
real app.

### Shape and spacing

| Token | Value |
|---|---|
| Spacing scale | 4, 8, 12, 16, 20, 24, 28, 32 px (8-pt grid, 4 for fine steps) |
| Radius | 14 inputs · 16 buttons, nav items · 20–24 cards · 28 panels · 999 pills · 50% round buttons |
| Touch target | ≥ 44 × 44 px (Apple minimum), primary record button 120 px |
| Panel padding | 24–28 px |

## 4. Layout

Three columns, as in Apple's sidebar apps (Notes, Mail):

| Column | Width | Holds |
|---|---|---|
| Sidebar | 220–280 px | navigation: New Session, Recent Sessions, To Review (count), Contacts; privacy note at the bottom |
| Content | flexible, min 560 px | the current task |
| Inspector | 280–340 px | "From the conversation": Speakers, Suggestions, summary |

Below ~1100 px the inspector moves under the content; on narrow windows the sidebar stacks on top.
The header holds only the brand, the "Processed locally" status and the theme toggle.

## 5. Components

| Component | Surface | States |
|---|---|---|
| Nav item | flat; current = `inset` + `accent-text` | hover: text darkens; current also has `aria-current="page"` |
| Segmented control (language) | track `inset`, selected pill `raised-sm` + bold text | `aria-pressed` on each option |
| Text field / select | `inset`, radius 14 | focus: accent outline; required and empty: `attention` ring + "To fill in" label |
| Secondary button | `raised-sm`, radius 16 | pressed: becomes `inset` |
| Primary button | solid `accent`, white text, `accent-lift` | disabled: becomes `inset` with `text-disabled` and a label saying why ("Fill in the time") |
| Record button | 120 px `raised` disc with an 84 px solid accent core | hover: core scales 1.04; label under it always visible |
| Status chip | `inset` pill | text says the state; color only reinforces it |
| Card | `raised`, radius 20–28 | rejected: 60 % opacity + "Rejected" label |
| Speaker tag | `raised-sm` pill, Speaker color text | Role name, never only a color |
| Skeleton | `raised-sm` bars inside an `inset` well | used while the Domain stage runs |

Icons: 1.8 px stroke, round caps and joins, 20 px in navigation (SF Symbols style). No emoji.

## 6. Screens and their rules

| Screen | Primary action | Rules |
|---|---|---|
| New Session | Start Session | Pick the language and the Contact before recording; importing an audio file is secondary. |
| Recording | Stop and analyse | Live Transcript is feedback only (no Proposed Actions while recording); Pause is secondary; timer and "Recording" always visible. |
| To Review | Approve and send, per Proposed Action | Each card shows the Action Type, Domain fields, the Transcript excerpt and **"What is sent · Connector"** with the exact payload (ADR 0002). Required empty fields block approval (OPEN-QUESTIONS #6). Reject and Undo are always available. Suggestions are read-only, in the inspector. |

## 7. Motion

Following Apple: short, purposeful, never decorative.

- 150–250 ms ease-out for state changes (press, select, card status).
- Recording dot and "speaking" dots pulse at 1.4 s.
- Respect `prefers-reduced-motion`: no pulsing, no scaling, instant state changes.

## 8. Accessibility checklist

- Real `<button>`, `<a href>`, `<input>` + `<label>`; nothing clickable is a `<div>`.
- All text ≥ 4.5:1 on its surface (3:1 only for text ≥ 24 px).
- Every state is readable without color and without shadow.
- Full keyboard path: Tab order follows the screen, Space/Enter activate, Esc closes dialogs.
- Visible focus on every control.
- `lang="ro"` on Romanian screens.

## 9. Open for the team

- Dark theme: values above are a starting point, not checked.
- Whether the frontend is plain HTML/CSS or React + TypeScript (`docs/ARCHITECTURE.md`, not decided).
- Fonts are bundled with the app (no Google Fonts at runtime: the app is local-first).
