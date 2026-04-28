# SRW PWA Asset Pack — historical mockup

> **This is the original asset-pack mockup.** The shipped artifacts live in:
> - `cockpit/public/` — favicons + manifest (root-served)
> - `cockpit/src/assets/icons/` — PWA icon set
> - `cockpit/src/assets/social/` — OG image
> - `cockpit/src/index.html` — head snippet (already pasted in)
> - `cockpit/src/assets/i18n/{en,de-DE}.json` — microcopy under the `pwa` namespace
>
> Treat this folder as the originals; if you regenerate the master SVG / icon sizes, update both this folder and the shipped copies.

Brand: **SRW · Cockpit** (Travertine theme)
Mark: Roman vexillum — cream tile, deep red banner, ivory SRW wordmark.

## What's in this pack

```
src/
├── favicon.ico
├── favicon.svg
├── favicon-16.png
├── favicon-32.png
├── favicon-48.png
├── manifest.webmanifest
└── assets/
    ├── icons/
    │   ├── icon.svg                       (master)
    │   ├── icon-mono.svg                  (Android themed icons)
    │   ├── icon-72…512.png                (all PWA sizes)
    │   ├── icon-maskable-192/512.png      (Android adaptive)
    │   ├── apple-touch-icon.png           (180×180, iOS)
    │   └── sc-jobs/create/sessions.png    (shortcut icons)
    └── social/
        ├── og-1200x630.svg
        └── og-1200x630.png

head-snippet.html
microcopy.json
```

## Install

1. Copy `src/` into your Angular project root.
2. Paste `head-snippet.html` contents into `src/index.html` <head>.
3. Ensure `angular.json` build assets includes the new files:
   ```json
   "assets": [
     "src/favicon.ico", "src/favicon.svg",
     "src/favicon-16.png", "src/favicon-32.png", "src/favicon-48.png",
     "src/manifest.webmanifest", "src/assets"
   ]
   ```
4. Wire `microcopy.json` into your i18n.

## Brand tokens

| Role             | Value     |
|------------------|-----------|
| Tile             | `#f3ece0` |
| Banner           | `#9c1f2e` |
| Staff / finial   | `#7a3b1a` |
| Ink              | `#fbf6ec` |
| theme_color      | `#9c1f2e` |
| background_color | `#f3ece0` |

## Notes

- favicon.ico is a 48×48 PNG-encoded ICO (modern-browser compatible). Run masters through `magick` if you need a true multi-res BMP ICO.
- iOS startup images omitted — Apple composites apple-touch-icon over background_color automatically.
- Mono SVG drives Android 13+ themed icons.
- Maskable variants pad into the safe zone for adaptive cropping.
