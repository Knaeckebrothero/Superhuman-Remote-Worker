"""Imperial (Travertine) brand colours for server-rendered surfaces.

Why literal hexes instead of reading the design tokens:

1. Email cannot use CSS custom properties. They sit at ~45% client support,
   and Gmail supports var() but not the variable *declaration* -- so colours
   must be resolved to literals at render time regardless.
2. docker/Dockerfile.orchestrator copies only orchestrator/, src/ and config/.
   The runtime has no access to cockpit SCSS even in principle.

tests/test_brand_palette.py parses the SCSS and fails closed if these drift.
Mirror of $travertine-theme in cockpit/src/styles/themes/_theme-config.scss.
"""

SCSS_TOKEN_SOURCE = "cockpit/src/styles/themes/_theme-config.scss"


def normalize_hex(value: str) -> str:
    """Lowercase and expand #abc shorthand to #aabbcc for stable comparison."""
    v = value.strip().lower()
    if len(v) == 4:  # '#abc'
        return "#" + "".join(c * 2 for c in v[1:])
    return v


TRAVERTINE: dict[str, str] = {
    "app-bg": "#f3ece0",  # travertine cream -- page ground
    "panel-bg": "#fbf6ec",  # card surface
    "surface-0": "#ede4d2",  # code / args block
    "border-color": "#dccfb6",
    "text-primary": "#2a1d12",  # deep umber
    "text-secondary": "#5a4632",
    "text-muted": "#8a7b66",
    "accent-color": "#9c2832",  # porphyry -- links, primary action
    "success": "#446b3e",  # laurel -- approve
    "danger": "#9c2832",  # blood -- deny
    "on-accent": "#ffffff",
}
