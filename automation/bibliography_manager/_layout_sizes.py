"""Layout size reference for the Bibliography Manager TUI.

Textual renders in a terminal grid of rows × columns.
This file documents every bottom-docked element's height so the
CSS `max-height` / `height` values stay correct.

─── Vertical stack (bottom → top) ─────────────────────────────

Layer                  Lines   Notes
─────────────────────  ─────   ────────────────────────────────
Footer (key bindings)      1   Built-in Textual Footer widget
margin-bottom              1   Pushes button-bar above Footer
#button-bar               6†  2 grid-rows × 3 lines/button
  Button row 1             3   border-top(1) + label(1) + border-bottom(1)
  Button row 2             3   same
Header                     1   Built-in Textual Header widget
─────────────────────  ─────
Total chrome               9   Minimum lines eaten by fixed UI
Dashboard (#dashboard)  rest   1fr — gets whatever remains

† grid-gutter vertical = 0, padding vertical = 0
  so 2 × 3 = 6 lines exactly for the button grid.
  max-height on #button-bar must be ≥ 7 (6 content + 1 margin-bottom).

─── Minimum terminal height ───────────────────────────────────

For a usable display:
  Header             1
  Stats bar          5   (border + 2 lines content + border + margin)
  Survey table       6   (min-height: 6 in CSS)
  Button bar         6   (2 rows of bordered buttons)
  margin-bottom      1   (clears the Footer)
  Footer             1
  ──────────────────
  MINIMUM           20 rows

Recommended: ≥ 30 rows for comfort.

─── CSS values that must stay in sync ─────────────────────────

  #button-bar  max-height  →  6   (exactly 2 × 3, no slack needed)
  grid-size                →  5 2 (5 columns, 2 rows = 10 buttons)
  grid-gutter              →  0 1 (0 vertical, 1 horizontal)
  Button height            →  3   (Textual default w/ border)
  Footer height            →  1   (Textual built-in)
  Header height            →  1   (Textual built-in)
"""
