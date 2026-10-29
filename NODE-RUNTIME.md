# Node runtime installation

Installed for the confirmed Apple Silicon / macOS Ventura 13.2.1 environment. Node **22.23.4 is now the default for new interactive zsh sessions**. The original Node 16 installation remains available explicitly and continues serving the existing scheduled jobs.

| Component | Version | Location |
| --- | --- | --- |
| Existing Node | v16.14.1 | `/usr/local/bin/node` |
| Existing npm | 8.5.0 | `/usr/local/bin/npm` (links to `/usr/local/lib/node_modules/npm/bin/npm-cli.js`) |
| nvm | 0.40.8 | `/Users/alejandroveloz/.nvm` |
| New Node | v22.23.4, arm64 | `/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node` |
| New npm | 10.9.9 | `/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/npm` |

The official pinned nvm installer ran with `PROFILE=/dev/null`, `NVM_INSTALL_VERSION=v0.40.8`, script installation method, and no automatic Node installation. Node was then installed with `nvm install -b --skip-default-packages 22.23.4`; the official `darwin-arm64` binary passed SHA-256 verification. No global packages were migrated. Installer locale warnings were nonfatal; no locale settings were changed.

nvm's default alias is pinned to exactly `22.23.4`. One standard initialization block was appended to `~/.zshrc`, preserving its existing bytes and behavior:

```sh
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"
```

`.zprofile`, `.zshenv`, system-wide PATH configuration, and scheduled jobs were not edited. New interactive Terminal sessions resolve `node` and `npm` to the versioned nvm directory, reporting v22.23.4 / 10.9.9. Open a new Terminal tab to use this default; already-open shells retain their current environment until reloaded or switched.

## Explicit use

```sh
/usr/local/bin/node --version
/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node --version
```

Run new npm with its matching Node without depending on PATH:

```sh
/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node \
  /Users/alejandroveloz/.nvm/versions/node/v22.23.4/lib/node_modules/npm/bin/npm-cli.js --version
```

Use `node` and `npm` normally in a new interactive zsh session. For an intentional temporary switch:

```sh
nvm use system
# Return to the pinned Node 22 default:
nvm use default
```

Both switches were verified in a fresh interactive login zsh, ending on Node 22. `nvm current` reported v22.23.4 and `nvm alias default` resolved to v22.23.4. No additional Node versions or packages were installed during this configuration change. Node 24 was not installed.

## yt-dlp configuration

The downloader should use the explicit executable `/usr/local/bin/yt-dlp-new` and pass:

```text
--js-runtimes node:/Users/alejandroveloz/.nvm/versions/node/v22.23.4/bin/node
```

This works without nvm being loaded or Node 22 being on PATH. Do not replace the versioned path with a shell-dependent `node` lookup. Future Node updates require explicitly reviewing/updating this path.

Metadata-only validation for `MbO7y_90zUI` succeeded with exit code 0. Verbose output recognized `node-22.23.4`, listed Node as an available challenge provider, and invoked the configured Node executable to solve JS challenges using bundled solver scripts v0.8.0. Extraction then completed without warnings or errors; the previous missing-runtime warning disappeared. This exercised the challenge-solving path successfully for this extraction, but does not establish media-download readiness for all videos.

Listed formats still include 720p30 AV1 `398` (`av01.0.05M.08`) and Opus audio `251`. No media, captions, comments, or thumbnails were downloaded. No extraction results were imported; inventory/classifications/decisions are unchanged. Ignored local evidence is under `state/node-install/` (installer log, raw metadata JSON, and verbose yt-dlp stderr); raw playback URLs there are temporary research evidence, not operational inventory.

## Preservation and rollback

Initial installation backups remain under `state/node-install/shell-backup/`. Immediately before the interactive configuration change, a timestamped byte-identical backup was created:

```text
/Users/alejandroveloz/.zshrc.before-nvm-20261029T051129751264Z
```

`state/node-install/interactive-before.json` and `interactive-change.json` record baseline checksums and the backup path. Verification confirmed cron and inventory bytes unchanged, `.zprofile`/`.zshenv` unchanged, original `.zshrc` bytes retained as a prefix, and exactly one nvm initialization block. All 22 offline tests passed.

The daily podcast scraper and both Saturday Apple Music jobs explicitly invoke `/usr/local/bin/node`; `.zshrc` is for interactive zsh and does not change those paths. `/usr/local/bin/node --version` still reports v16.14.1. Homebrew, launchd, sibling projects, scheduled scripts, and global npm packages were not modified.

To restore Node 16 as the default while keeping nvm available:

```sh
nvm alias default system
nvm use system
```

For a full shell-configuration rollback, also restore the exact backup and open a new Terminal tab:

```sh
cp /Users/alejandroveloz/.zshrc.before-nvm-20261029T051129751264Z /Users/alejandroveloz/.zshrc
```

These are documented rollback commands, not actions performed during verification. After restoration, verify `command -v node` is `/usr/local/bin/node` and `node --version` is v16.14.1. The explicit Node 22 path used by future unattended yt-dlp operations is independent of the interactive default and remains installed unless separately removed.

Optional future removal, only when requested: restore the system default, switch to `system`, use `nvm uninstall 22.23.4`, and retire any downloader configuration pointing to that executable. Restore `.zshrc` before removing the nvm directory separately if desired. Never remove or overwrite `/usr/local/bin/node` as part of rollback.
