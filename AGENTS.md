# pvr-inox-radar

Agent skill that turns a movie-night ask ("Dune, IMAX, Saturday night, 4
seats together, under 30 min from Sector 56 Gurgaon") into one self-contained
HTML map of PVR INOX shows with verified seats-together counts and deep links
into PVR's own booking flow.

- The skill lives at `skills/pvr-inox-radar/SKILL.md` (Agent Skills standard:
  works in Claude Code, Codex CLI, and Gemini CLI). Codex discovers it
  automatically inside this repo via `.agents/skills`.
- No install step is needed to use the skill from inside this repo: read
  `SKILL.md` and invoke the scripts by path. Installing only copies the skill
  somewhere an agent auto-discovers it, globally under `$HOME` by default, or
  into one project with `install.sh --here` / `--project DIR`.
- `DEFAULT_REPO` in `install.sh` must always name THIS repo, so any copy
  installs its own reviewed code by default.
- Iron rules live in SKILL.md and are enforced by the client: minimum 0.6s
  between API calls, strictly sequential, stop on 403/429 (15 minute
  persisted cooldown), read-only (never book, never poll), and credit to
  notprashanth/pvr-inox-mcp (MIT) for the API mapping and the withheld-rows
  insight. Never weaken any of these.
- All scripts are stdlib-only Python 3.9+. No pip installs, ever.
- Run the offline suite before changing any script:
  `python3 -m unittest discover -s tests`
  (zero network; fixtures and mocked transport). The opt-in live smoke test
  is `PVR_LIVE=1 python3 -m unittest tests.live_smoke` and needs an explicit
  live-call budget.
- No em or en dashes anywhere in authored files or commit messages; the
  hygiene test enforces this repo-wide.
