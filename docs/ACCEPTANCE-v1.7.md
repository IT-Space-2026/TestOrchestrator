# v1.7 acceptance mapping — About and IT-Space branding

Spec: `testorchestrator-specification-v1.7.md` (normative addition to
v1.0–v1.6). Status of each acceptance item and where it is verified.

| ID | Requirement | Implementation | Verified by |
|---|---|---|---|
| AB01 | All four roles can open About | `About` item in the sidebar-footer user menu (all roles); opens in-app dialog; dedicated `/about` page as no-JS fallback | `test_ab01_*` (parametrized over all roles) |
| AB02 | Exact `IT-Space Kft.` name, approved logo, HTTPS homepage | Company text constants in `testorchestrator/main/routes.py`; `https://it-space.info` shown verbatim, `target="_blank" rel="noopener noreferrer"` | `test_ab02_about_content` |
| AB03 | Renders with internet blocked; no external requests until homepage click | All CSS/JS/logo local; homepage link is navigation only; CSP `default-src 'self'` blocks external sub-resources | `test_ab03_*` |
| AB04 | Version/build match installed release metadata | `release/release.json` bundled per payload; loaded once at startup by `testorchestrator/release.py`; missing/invalid metadata fails loudly | `test_ab04_*` |
| AB05 | No restricted audit/changelog/admin data; notices readable locally | Changelog shortcut rendered only for `role == ADMIN`; `/system/*` behind `role_required(ADMIN)`; `/about/notices` + `/3rdparty/<file>` serve bundled licence texts | `test_ab05_*` |
| AB06 | Logo packaged locally, no distortion, alt text, keyboard accessibility | SVG in `static/branding/` with `width`/`height` preserving aspect ratio and own background for light/dark contrast; `alt="IT-Space Kft. logo"`; dialog `role="dialog" aria-modal="true"`, focus trap, Esc close + focus return (`static/js/about.js`) | `test_ab06_logo_and_dialog_accessibility` |
| AB07 | Missing/unapproved official logo recorded as incomplete, not silently substituted | `assets/branding/README.md` approval table; UI note "Temporary placeholder"; `LOGO_APPROVED = False` flag | `test_ab07_*` |

## Behaviour/security notes

- About works offline: dialog markup is server-rendered into every page;
  opening it performs no request at all (not even a local fetch).
- No credentials, hostnames/IPs, licence identifiers, internal topology, or
  secrets are displayed in About.
- Focus returns to the invoking control on close; small-screen layout via
  `max-width: 40rem` media query; both light and dark themes supported and
  the placeholder keeps its own high-contrast background in both.
- Company text comes from the owner requirement verbatim; no registration
  numbers, addresses, legal claims, or copyright wording were added.
