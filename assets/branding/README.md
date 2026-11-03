# Branding assets — approval tracking

This directory tracks owner-supplied / owner-approved branding assets,
separate from `3rdparty/` (third-party notices are not replaced by company
branding).

## Status

| Asset | Status |
|---|---|
| Official IT-Space Kft. logo | **SUPPLIED** — `iT-space-logo-400x400.png` (1667x1667 RGBA PNG, transparency); served from `testorchestrator/static/branding/iT-space-logo.png` |

## Placeholder

`testorchestrator/static/branding/it-space-logo.placeholder.svg` was the
development-only placeholder; the official asset now ships alongside it
and is the active logo. It is **not** the
official IT-Space Kft. logo and makes no claim to be.

Per specification v1.7 (AB07): production branding acceptance is **blocked**
until an owner-approved asset lands here. Do not invent, redraw, or fetch a
logo from the web — text retrieval from the homepage does not constitute a
verified downloaded logo.

## Acceptance requirements for the real asset

- Approved by the owner; tracked in this directory.
- Sanitized SVG and/or PNG, aspect ratio preserved, clear contrast on both
  light and dark themes.
- Packaged locally and loaded with descriptive alt text
  "IT-Space Kft. logo".
- No hotlinking, tracking pixels, external fonts, or CDN delivery.
