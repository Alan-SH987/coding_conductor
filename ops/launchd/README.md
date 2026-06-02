# Always-on dev servers (launchd)

Keeps the Coding Conductor dev servers from "suddenly vanishing" when a terminal
or session closes. Installs two macOS **LaunchAgents** (run as you, no root):

| Service | Port | Command |
| --- | --- | --- |
| `com.codingconductor.backend` | 8010 | `uvicorn app.main:app --reload` |
| `com.codingconductor.frontend` | 3000 | `npm run dev` (NEXT_PUBLIC_API_BASE=:8010) |

Both have `RunAtLoad` (start at login) and `KeepAlive` (auto-restart on crash),
so they survive a closed terminal, a crash, and a reboot.

> ⚠️ **Does not work while this repo lives under `~/Desktop`.** macOS TCC blocks
> LaunchAgents from reading `~/Desktop` (and `~/Documents`, `~/Downloads`), so the
> agents crash-loop, unable to even open the launcher (`can't open input file`).
> Use this only after moving the repo off `~/Desktop`, or grant `/bin/zsh` Full
> Disk Access. For a Desktop repo, prefer the terminal-resident setup in
> [`../serve/`](../serve/README.md).

## Use

```sh
./ops/launchd/install.sh      # generate plists in ~/Library/LaunchAgents + load
./ops/launchd/uninstall.sh    # stop + remove them

launchctl list | grep codingconductor          # status (PID, last exit code)
tail -f .conductor/logs/com.codingconductor.backend.log    # logs (gitignored)
```

After editing this repo's code, the backend (`--reload`) and Next.js pick changes
up automatically — no manual restart. To restart by hand:

```sh
launchctl kickstart -k gui/$(id -u)/com.codingconductor.backend
```

## Notes

- **PATH:** the plists run the launchers via `zsh -lc`, which sources your login
  profile so `git` / `node` / `claude` / `codex` are found. launchd's own
  environment is minimal and would not find them otherwise.
- **One owner at a time:** don't also run the servers manually in a terminal
  while these agents are loaded — both would fight for ports 8010 / 3000. Run
  `uninstall.sh` first if you want to go back to a manual terminal workflow.
- **Not `--reload`?** For a pure always-on server (no file watching), drop
  `--reload` from `run-backend.sh` and re-run `install.sh`.
