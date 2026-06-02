# Always-on dev servers (terminal-resident)

Keeps the backend (:8010) and frontend (:3000) running after you close the
terminal, and restarts them if they crash — without launchd.

Why not launchd? This repo lives under `~/Desktop`, which macOS TCC protects.
A background LaunchAgent can't read `~/Desktop`, so it can't even start the
servers (see `../launchd/` for that path — it works only if the repo is moved
off Desktop, or `/bin/zsh` is granted Full Disk Access). Started from a terminal
instead, the servers inherit your shell's PATH **and** Desktop access.

## Use

```sh
./ops/serve/start.sh     # start (detached, self-restarting)
./ops/serve/stop.sh      # stop
tail -f .conductor/logs/backend.log      # logs (gitignored)
```

- **Survives terminal close:** `nohup` + `disown`.
- **Restarts on crash:** each server runs under `_supervise.sh` (a restart loop).
- **Not boot-automatic:** re-run `start.sh` after a reboot. (For boot-start you'd
  need the launchd route — i.e. move the repo off `~/Desktop` first.)
- One owner at a time: don't also run the servers manually elsewhere; they'd
  fight for ports 8010 / 3000. `stop.sh` first.
