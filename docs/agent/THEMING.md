# Appearance and Theme Customization

Use the theme helper for supported palette, font preset, spacing density, and corner-radius changes. It validates readable combinations and confines writes to the generated theme stylesheet. This playbook changes appearance only: it does not rename the product, redesign page structure, add logos/icons, or alter financial behavior.

## Focused Interview

Ask only what is needed:

1. What should the interface feel like: calm, bright, restrained, warm, playful, or something else?
2. Preferred primary/accent color?
3. Light mode, dark mode, or both?
4. Comfortable or compact spacing?
5. System, humanist, rounded, or serif font character?
6. Softer rounded controls or sharper corners?
7. Any contrast, color-vision, motion, or readability requirement?

Translate answers into the ignored `agent-config/theme-profile.local.json`, copied from `agent-config/theme-profile.example.json`. Use fictional/general preference descriptions; a theme profile should contain no financial or identifying information.

## Baseline and Apply

From the repository root, validate the tracked example and current generated stylesheet before making a theme change:

```powershell
python tools/customize_theme.py --profile agent-config/theme-profile.example.json --check
```

Then validate and preview the personal profile without writing:

```powershell
python tools/customize_theme.py --profile agent-config/theme-profile.local.json --check
python tools/customize_theme.py --profile agent-config/theme-profile.local.json --preview
```

Apply only after the profile is valid and the user intent is clear:

```powershell
python tools/customize_theme.py --profile agent-config/theme-profile.local.json --apply
```

`--check` and the default invocation do not write. `--preview` prints the resulting CSS. `--apply` refuses to write anywhere except `current/app/static/theme.generated.css`.

After applying, inspect the diff. A normal supported theme experiment modifies the generated stylesheet only. If unrelated templates, financial code, database files, or user data appear in the diff, stop and separate the work.

If the customized theme will be included in a shared repository, the generated CSS must have a tracked, reproducible source profile. After the owner reviews the non-sensitive local profile, copy its confirmed values into the tracked `agent-config/theme-profile.example.json`, apply from that tracked profile, and run `--check` against it. Do not commit the ignored personal profile. If the theme is only a private experiment, do not share generated CSS without its reviewed source profile.

## Supported Tokens

Each light/dark palette supplies:

- primary and on-primary text;
- page background and main text;
- surface and alternate surface;
- border and muted text;
- success, danger, and warning emphasis colors.

The helper requires hex colors, validates text contrast for body, surfaces, and primary controls, derives hover/focus variants, and emits Bootstrap-compatible RGB variables. Font choices are allow-listed presets rather than arbitrary CSS. Generated CSS loads after Bootstrap and the base application stylesheet so supported tokens win without a repository-wide color replacement.

## Visual Verification

Use a fresh fictional demo workspace. Review both light and dark modes unless the user explicitly limited scope. Check:

- navigation, page background, headings, links, and buttons;
- form controls, invalid states, keyboard focus rings, and disabled states;
- alerts, badges, tables, cards, modals, and pagination;
- dashboard, transaction/import review, savings, reconciliation, and investment chart screens;
- a narrow mobile viewport and a typical desktop viewport;
- meaning that might be conveyed by color alone;
- text size, wrapping, clipping, and dense numeric tables.

Capture only synthetic screenshots. The current investment chart listens for theme changes; verify chart labels and controls in both modes.

## When the Request Is Larger

Use the branding playbook when the request changes the visible product name or terminology. The visible name is controlled by `APP_DISPLAY_NAME` and remains separate from stable data-directory slugs, local-storage keys, JavaScript event names, service/package IDs, and other machine-facing identity.

Treat layout, navigation, logo, icons, illustrations, component redesign, or arbitrary CSS as a separate UI task with its own acceptance scenarios and browser review. Do not expand the theme helper to accept unbounded CSS merely to fit that request.

## Completion Report

Report the theme profile used, helper commands run, generated file changed, modes/viewports/screens reviewed, accessibility considerations, and synthetic screenshots produced. State any requested visual work intentionally deferred to a separate UI task.
