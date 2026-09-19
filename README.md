# zcode-snapshot-disabler

Disable the code-snapshot upload pipeline in the [ZCode](https://zcode.ai)
desktop app — one equal-length, fully reversible in-place patch.
Windows / macOS / Linux.

[简体中文说明](README.zh-CN.md)

## What it does

Before certain prompts, the ZCode desktop client (an Electron app) scans your
workspace, packs a code snapshot, and uploads it (an upload-credential request
followed by the upload itself). If you don't want your source code leaving your
machine, this toolkit flips **one boolean gate** inside `app.asar` so that
pipeline returns early — the exact code path the app itself already uses when
you are **not logged in**:

- no snapshot is packed or uploaded
- no upload-credential request is sent
- the archive stays byte-for-byte the same size and structure

Everything else in the app keeps working. The change is fully reversible with
one command.

## Compatibility

The patch is **pattern-based**: it locates the gate by code structure (not by
hardcoded offset) and tolerates minified variable renames, so routine app
updates keep working. The app's version is detected at runtime and compared
against the last-patched one; if a future refactor breaks the match, the tool
reports `no-gate` and refuses to touch the file.

Verified:

| ZCode version | Platform                                  | Result                                    |
| ------------- | ----------------------------------------- | ------------------------------------------ |
| 3.12.3        | Windows x64, all-users (Program Files)    | patched; `--selftest` round-trip passed    |

macOS / Linux code paths are implemented and reviewed but not yet validated
on a real machine — run `--scan` first there and report what you see.

## How it works

All snapshot triggers (prompt, terminal, repo-wiki) funnel into a single
function inside `app.asar`:

```js
async captureBeforePromptUnsafe(t) {
    t.signal?.throwIfAborted();
    let r = await this.tokenProvider();          // local token read, no network
    if (t.signal?.throwIfAborted(), !r) return;  // <- native logged-out guard
    ... scan / pack / getUploadKey / upload ...
}
```

The patch rewrites the gate operand `!r` to the **equal-length always-truthy**
form `!0`, so the function returns exactly like the native logged-out path.

Key properties:

- **Equal-length, in-place edit.** File size and every asar header offset stay
  valid; the archive remains structurally intact. Endpoint strings are never
  touched.
- **Minifier-proof.** The regexes match any minified variable name
  (`!r`, `!ab`, …) and re-derive it from the adjacent assignment, so the tool
  survives routine rebuilds. For an n-char variable the replacement is `!`×n
  plus `0` (n odd) or `1` (n even) — always truthy, always n+1 bytes.
- **Idempotent in both directions.** Patched forms (`!0`, `!!1`, …) can never
  re-match the unpatched pattern, so running twice never double-patches.
- **Self-verifying.** Exact offset compare before and after the write, plus an
  independent post-run rescan. If anything is off, the file is left untouched.
- **State + log.** Each edit is recorded in `patch-state.json` (offset,
  original bytes); restore works even without it, from the surrounding context.
- **Version-aware.** The app's own version is parsed from `package.json`
  inside the archive (proper asar header parse — never a loose grep that
  could pick up a bundled dependency's version). It is shown in logs and
  `--scan`, recorded in `patch-state.json`, and compared against the
  last-patched version, so an app update that replaced `app.asar` is
  called out explicitly.

## Quick start

Requires Python 3.6+ and nothing else (standard library only).

```bash
python3 patch_asar.py --scan      # list discovered installs + gate status (read-only)
python3 patch_asar.py             # discover + patch every copy (idempotent)
python3 patch_asar.py --dry-run   # show planned edits, write nothing
python3 patch_asar.py --selftest  # patch -> restore -> patch round-trip per target
python3 restore_asar.py           # undo everything
```

Then **restart ZCode** — a running process keeps its old code until relaunched.
If your system `python` is 2.x on Windows, the scripts re-exec themselves via
`py -3` automatically.

### One-line AI prompt

Prefer to have your AI coding agent do it? Paste this:

```text
Disable ZCode's code-snapshot upload on this machine: clone
https://github.com/pjpv/zcode-snapshot-disabler, run patch_asar.py with
Python 3 (--scan first to inspect, then patch — approve the UAC / sudo /
osascript prompt when it appears), re-run --scan to verify patched=1,
report the detected app version, and remind me to restart ZCode; if I ever
want to undo, run restore_asar.py.
```

## Platform coverage

| Platform / install            | Discovered at                                          | Elevation          |
| ----------------------------- | ------------------------------------------------------ | ------------------ |
| Windows, all-users            | `%ProgramFiles%\*zcode*\resources\app.asar`            | UAC (automatic)    |
| Windows, per-user NSIS        | `%LOCALAPPDATA%\Programs\*zcode*\...`                  | usually none       |
| Windows, legacy Squirrel      | `%LOCALAPPDATA%\*zcode*\app-*\...`                     | none               |
| Windows, scoop                | `~/scoop/apps/*zcode*/current/...`                     | none               |
| Windows / portable, any path  | —                                                      | `--asar <path>`    |
| macOS                         | `/Applications/ZCode.app`, `~/Applications/...`        | osascript prompt   |
| Linux, deb/rpm                | `/opt`, `/usr/lib`, `which zcode` walk-up              | sudo / pkexec      |
| Linux, AppImage               | PATH + common dirs                                     | extract → patch    |

All copies found on a machine are patched (nothing slips through); `--asar`
pins specific targets (repeatable; accepts an `app.asar` file, an install
directory, or an AppImage).

Not supported, skipped with a message: Windows MSIX/Store installs
(WindowsApps ACL) and snap packages (read-only squashfs).

## Privilege model

The parent process discovers targets in **your** context and patches the
user-writable ones itself. Targets needing root are handed to an elevated
child as exact absolute paths — the child never re-discovers (root's
HOME/PATH would see less than yours) — and reports back through a JSON marker
at a random temp path. State and log files are only ever written by the
unprivileged parent, so the toolkit directory never contains root-owned
files. One elevation prompt per invocation at most.

## Notes

- **macOS**: editing inside a `.app` invalidates its code signature, so the
  script re-signs ad-hoc (`codesign --force --deep -s -`) after every edit —
  no developer account needed. If the first launch complains, run
  `xattr -dr com.apple.quarantine /Applications/ZCode.app`.
- **AppImage**: the inner squashfs is read-only; the script extracts to
  `<AppImage>.patched/` next to it and patches there. Launch via that
  directory's `AppRun`; the original AppImage file is left as-is.
- **App updates replace `app.asar`** — re-run `patch_asar.py` afterwards
  (`--scan` first if you want to see what changed). A `no-gate` result means
  the pattern no longer matches this version; the tool refuses to touch the
  file and the gate needs re-locating.
- Applying the patch while the app is running is safe (equal-length rewrite),
  but takes effect only after a restart.
- Exit codes: `0` success · `1` at least one target failed · `2` no targets
  found.

## Restore

```bash
python3 restore_asar.py           # all copies
python3 restore_asar.py --asar "C:\Program Files\ZCode\resources\app.asar"
```

The original minified variable name is re-derived from the assignment feeding
the gate, so restoration is precise even if `patch-state.json` is missing or
stale. Exits cleanly when there is nothing to restore.

## Disclaimer

This is a personal-privacy tool: it modifies an application installed on your
own machine so that it stops uploading your code. It is not affiliated with
or endorsed by the app's vendor, modifying the application may conflict with
its terms of service, and you use it at your own risk. Always keep
`restore_asar.py` within reach.

## License

[MIT](LICENSE)
