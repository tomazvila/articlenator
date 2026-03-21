for everything nix develop must be used, there are no global dependencies locally
after changes done, increment build version, it must be shown in the footer too
every change must be implemented top, to bottom:
- first the research on best ways to do it
- then mocks to implement the structure
- then tests covering both the negative and positive space and edge cases
- then scafolding
- then implementation till tests pass
- then very imortant most important, click test with playwright mcp to verify changes work end to end from client and user perspective when clicking through browser

if there is missing data, session tokens you must ask for missing data
if there is missing dependency it must be added and manage through nix flake

before commiting any changes must run the linter

## Running the app

- `nix develop` then `python -m twitter_articlenator` — dev mode (uses local src/)
- `nix run` — run the built package directly

## Design Context

### Users
Single developer (LilVilla) using this as a personal self-hosted tool. Converts Twitter/X articles and bookmarks into e-reader PDFs, downloads videos. Power-user who values function over flash.

### Brand Personality
**Dark, literary, refined.** A digital reading room — the B&W library tunnel background is central to the identity. Leans into the bookish, archival quality of collecting and preserving articles.

### Aesthetic Direction
- **Theme**: Tokyo Night color palette (dark mode only). References: Obsidian, Neovim — information-dense power-user tools with dark themes.
- **Background**: The library tunnel bg.png is a core brand element and must always be present. It requires treatment (overlay, opacity, blur, or darkening) to ensure text legibility over it. Content areas should have sufficient contrast against the background.
- **Typography**: System fonts for body, JetBrains Mono / Fira Code for monospace/code elements.
- **Layout**: Single centered column (800px max), card-based main content area with semi-opaque backgrounds.

### Design Principles
1. **Background is identity, not decoration** — The library tunnel bg.png stays. Adjust overlays and content backgrounds to ensure legibility, never remove the image.
2. **Legibility over atmosphere** — When background and readability conflict, readability wins. Use sufficient backdrop opacity/blur on content areas.
3. **Quiet utility** — The interface should stay out of the way. No unnecessary ornamentation. Every element earns its place.
4. **Respect the palette** — Stay within Tokyo Night variables. Tint, shade, and alpha-adjust them rather than introducing new colors.
5. **Accessible basics** — Target WCAG AA contrast (4.5:1 for text), semantic HTML, keyboard navigable. Reasonable focus indicators.
