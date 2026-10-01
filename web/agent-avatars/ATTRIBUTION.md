# Agent characters

The four local SVGs are composed from original vector paths in **Monster Builder
Pack 1.0**, created and distributed by **Kenney** (2022). The artwork is released
under **Creative Commons Zero 1.0 (CC0)** and may be modified and redistributed
for personal, educational and commercial projects. Attribution is optional; we
retain it here. The bundled original license is `LICENSE-Kenney.txt`.

- Official asset page: https://kenney.nl/assets/monster-builder-pack
- Official license explanation: https://kenney.nl/support
- CC0 terms: https://creativecommons.org/publicdomain/zero/1.0/
- Original archive: https://kenney.nl/media/pages/assets/monster-builder-pack/663e4ef6de-1677495438/kenney_monster-builder-pack.zip
- Archive SHA256: `6e64b7979499c7465f0ce1abfdde91db057875bda50ece61b47aa0c0005dd566`
- Retrieved: 2026-10-01. Only selected paths are bundled; no asset pack dependency.

Changes: selected body, arm, leg, eye and mouth paths from `Vector/overview.svg`
are normalized, assembled, recolored to the dashboard palette, and given a small
ground shadow. `moss.svg` is round with one eye; `aqua.svg` is square with two
eyes; `coral.svg` is tall with two eyes; `amber.svg` is pear-shaped with three
eyes. The character names describe artwork, not configured agent identities.
The original CSS animation and layout are part of PC Remote's project license.

## Integration

Include `/agent-avatars.css`. Use decorative artwork beside an actual visible
agent name and status, for example:

```html
<span class="agent-avatar agent-avatar--moss" data-state="offline" aria-hidden="true">
  <img src="/agent-avatars/moss.svg" alt="" width="96" height="96">
</span>
```

Apply `data-state` to the wrapper, using only verified agent data: `idle`,
`working`, `waiting`, `thinking`, `attention`, or `offline`. A missing/`unknown` state is
still and shows a question mark. Never translate a connected process into
working/thinking without a corresponding event. These are visual state cues;
they do not infer emotions. `agent-avatar--small` gives a 64 px version.

All resources are local; SVGs contain no scripts, links, embedded images or
external references. No JavaScript, fonts, animation library or runtime network
request is needed. Reduced-motion disables all animation. The static preview
labels all states as demonstration examples, never live telemetry.
