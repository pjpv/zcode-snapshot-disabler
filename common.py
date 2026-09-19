# -*- coding: utf-8 -*-
"""Cross-platform core for the ZCode snapshot-gate patch (defence kit).

Target (single copy inside app.asar; prompt / terminal / repo-wiki
triggers all funnel into it):

    async captureBeforePromptUnsafe(t) {
        t.signal?.throwIfAborted();
        let r = await this.tokenProvider();           // local JWT read, no network
        if (t.signal?.throwIfAborted(), !r) return;   // <- not-logged-in guard
        ... scan / pack / getUploadKey / upload ...
    }

The patch rewrites the gate operand "!r" to the equal-length always-truthy
form "!0", so the pipeline returns exactly like the native logged-out
path: zero network requests, no 404 signatures.  Equal length keeps every
asar header offset valid, so the archive stays structurally intact.

Platform coverage:
    Windows: all-users (Program Files, UAC), per-user NSIS / Squirrel /
             scoop (usually no elevation), portable via --asar
    macOS:   .app bundles, osascript admin prompt, ad-hoc codesign after edit
    Linux:   /opt /usr/lib style installs (sudo / pkexec), AppImage via
             extract-and-patch
    Not supported: Windows MSIX/Store (WindowsApps ACL), snap (read-only
             squashfs) -- both are skipped with a message, never attempted

Privilege model (see run_elevated / write_marker):
    the parent discovers targets, decides who handles what, writes state
    and the log.  The elevated child receives the exact asar paths to edit
    via argv (no re-discovery: root's HOME/PATH would find less than the
    user's), edits them, and reports through a JSON marker at an
    mkstemp-random path in the invoking user's temp dir.

Version handling: the app's own version is read from package.json inside
the archive (proper asar header parse -- never a loose grep that could
pick up a bundled dependency's version), shown in logs, recorded per
target in the state file, and compared against the last-patched version
so an app update is called out explicitly.
"""
import glob
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time

DEFENCE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(DEFENCE_DIR, "patch-state.json")
LOG_PATH = os.path.join(DEFENCE_DIR, "patch_asar.log")

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# ---------------------------------------------------------------- patterns

# Anchor shared by both states: the assignment directly feeding the gate.
ANCHOR = b"this.tokenProvider();if("

# Unpatched span: assignment ... gate.  group(1) = assigned var,
# group(2) = gate operand var (must equal group(1)).  Identifiers cannot
# start with a digit, so already-patched forms (!0, !!1, ...) can never
# re-match this pattern -- that is the idempotency guarantee.
TARGET_UNPATCHED = re.compile(
    rb"([A-Za-z_$][A-Za-z0-9_$]*)=await this\.tokenProvider\(\);"
    rb"if\([^;]*?throwIfAborted\(\),!"
    rb"([A-Za-z_$][A-Za-z0-9_$]*)"
    rb"\)return;"
)

# Patched span: the operand became an alternating-negation truthy literal
# (!0, !!1, !!!0, ... -- always true by construction, see truthy_form()).
TARGET_PATCHED = re.compile(
    rb"([A-Za-z_$][A-Za-z0-9_$]*)=await this\.tokenProvider\(\);"
    rb"if\([^;]*?throwIfAborted\(\),"
    rb"(!+)([01])"
    rb"\)return;"
)


def truthy_form(var):
    """Equal-length always-true replacement for b'!' + var (any var length).

    n negations over a falsy base (0) yield true iff n is odd; over a
    truthy base (1) iff n is even.  So: odd n -> '!'*n + '0', even n ->
    '!'*n + '1'.  n=1 gives the canonical '!0'.
    """
    n = len(var)
    form = b"!" * n + (b"0" if n % 2 == 1 else b"1")
    if len(form) != n + 1:
        raise AssertionError("length invariant broken")
    return form


# ---------------------------------------------------------------- child io

_CHILD_MARKER = None
_CHILD_LOG = None


def set_child_io(marker, childlog):
    global _CHILD_MARKER, _CHILD_LOG
    _CHILD_MARKER = marker
    _CHILD_LOG = childlog


def results_ok(results):
    """Single exit-code criterion shared by the direct path, the elevated
    child (marker) and the parent, so they can never diverge."""
    for r in results or ():
        if r.get("status") in ("fail", "no-gate"):
            return False
        if r.get("codesign") == "FAILED":
            return False
    return True


def _chown_sudo(path):
    """sudo direct run: hand a defence/ file back to the invoking user.
    SUDO_UID is absent under osascript/pkexec -- nothing to do then."""
    if IS_WINDOWS or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    uid = os.environ.get("SUDO_UID", "")
    if not uid.isdigit():
        return
    try:
        os.chown(path, int(uid), -1)
    except OSError:
        pass


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line)
    except UnicodeEncodeError:  # legacy-codepage console / piped stdout
        print(line.encode("ascii", "replace").decode("ascii"))
    target = _CHILD_LOG if _CHILD_LOG else LOG_PATH
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _chown_sudo(target)
    except OSError:
        pass


def _edit_json(e):
    """One edit entry in JSON-safe form (bytes -> str, off -> offset)."""
    old = e.get("old", e.get("original"))
    new = e.get("new", e.get("patched"))
    if isinstance(old, bytes):
        old = old.decode("ascii")
    if isinstance(new, bytes):
        new = new.decode("ascii")
    return {"offset": e.get("off", e.get("offset")), "var": e.get("var"),
            "original": old, "patched": new}


def write_marker(results):
    """Elevated child reports back: results = list of per-target dicts."""
    if not _CHILD_MARKER:
        return
    payload = {"ok": results_ok(results), "results": [
        {"path": r.get("path"), "status": r.get("status"),
         "version": r.get("version"),
         "error": r.get("error"), "codesign": r.get("codesign"),
         "edits": [_edit_json(e) for e in r.get("edits", [])]}
        for r in results]}
    try:
        with open(_CHILD_MARKER, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as exc:  # never crash the child over the marker
        print("marker write failed: %r" % (exc,))
        try:
            with open(_CHILD_MARKER, "w", encoding="utf-8") as f:
                f.write("OK" if payload["ok"] else "FAIL")
        except OSError:
            pass


# ---------------------------------------------------------------- discovery

def _dedupe(paths):
    seen, out = set(), []
    for p in paths:
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _win_roots():
    pats = [
        r"%ProgramFiles%\*zcode*\resources\app.asar",
        r"%ProgramFiles(x86)%\*zcode*\resources\app.asar",
        r"%ProgramW6432%\*zcode*\resources\app.asar",
        r"%LOCALAPPDATA%\Programs\*zcode*\resources\app.asar",
        r"%LOCALAPPDATA%\*zcode*\app-*\resources\app.asar",
        r"%LOCALAPPDATA%\*zcode*\resources\app.asar",
        r"~/scoop/apps/*zcode*/current/resources/app.asar",
    ]
    found = []
    for pat in pats:
        found += glob.glob(os.path.expandvars(os.path.expanduser(pat)))
    return found


def _posix_roots():
    found = []
    for base in ("/opt", "/usr/lib", "/usr/share",
                 "/usr/local/lib", "/usr/local/share"):
        for nm in ("ZCode", "zcode"):
            p = os.path.join(base, nm, "resources", "app.asar")
            if os.path.isfile(p):
                found.append(p)
    # which zcode -> resolve symlink (native install or AppImage)
    w = shutil.which("zcode")
    if w:
        rp = os.path.realpath(w)
        if rp.lower().endswith((".appimage",)):
            found.append(rp)
        else:
            d = os.path.dirname(rp)
            for _ in range(4):
                cand = os.path.join(d, "resources", "app.asar")
                if os.path.isfile(cand):
                    found.append(cand)
                    break
                d = os.path.dirname(d)
    # .app bundles
    for base in ("/Applications", os.path.expanduser("~/Applications")):
        for nm in ("ZCode.app", "zcode.app"):
            p = os.path.join(base, nm)
            if os.path.isdir(p):
                found.append(p)
    # AppImages in common spots
    for d in ("~/Applications", "~/bin", "~/.local/bin", "~/Downloads"):
        base = os.path.expanduser(d)
        if os.path.isdir(base):
            found += [m for m in glob.glob(os.path.join(base, "*"))
                      if m.lower().endswith((".appimage",))
                      and "zcode" in os.path.basename(m).lower()]
    return found


def discover():
    """Return install roots: app.asar files, .app directories, AppImages.
    Runs in the INVOKING user's context -- paths found here are handed to
    the elevated child verbatim, never re-discovered as root."""
    raw = _win_roots() if IS_WINDOWS else _posix_roots()
    return [p for p in _dedupe(raw) if os.path.exists(p)]


def asars_under_dir(d, maxdepth=6):
    """All resources/app.asar copies under a directory (.app, portable)."""
    out = []
    base_depth = d.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(d):
        if root.count(os.sep) - base_depth > maxdepth:
            dirs[:] = []
            continue
        if "app.asar" in files and os.path.basename(root).lower() == "resources":
            out.append(os.path.join(root, "app.asar"))
    return out


def appimage_extract(img):
    """Extract an AppImage and return its app.asar path(s).

    The squashfs inside an AppImage is read-only, so we extract once into
    a stable directory and patch there.  Launch afterwards via the
    extracted AppRun (the original AppImage itself stays unpatched)."""
    stem = os.path.splitext(os.path.basename(img))[0]
    parent = os.path.dirname(os.path.abspath(img))
    dest = os.path.join(parent, stem + ".patched")
    if not os.access(parent, os.W_OK):
        dest = os.path.join(DEFENCE_DIR, "appimage", stem)
    existing = asars_under_dir(dest)
    if existing:
        log("reusing existing extraction %s -- delete it to re-extract "
            "after an AppImage update" % dest)
        return existing
    work = tempfile.mkdtemp(prefix="zcode-defence-")
    try:
        orig_mode = os.stat(img).st_mode
        os.chmod(img, orig_mode | 0o111)  # AppImage runtime needs +x
        try:
            r = subprocess.run([img, "--appimage-extract"], cwd=work,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            os.chmod(img, orig_mode)      # leave the original as found
        sr = os.path.join(work, "squashfs-root")
        if r.returncode != 0 or not os.path.isdir(sr):
            log("AppImage extract failed: %s" % img)
            return []
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.move(sr, dest)
    except OSError as exc:
        log("AppImage extract error: %r" % (exc,))
        return []
    finally:
        shutil.rmtree(work, ignore_errors=True)
    asars = asars_under_dir(dest)
    if asars:
        log("AppImage extracted to %s -- launch via its AppRun" % dest)
    return asars


def _unsupported_reason(path):
    low = os.path.normpath(path).replace("\\", "/").lower()
    if "windowsapps" in low:
        return "MSIX/Store install (WindowsApps ACL)"
    if not IS_WINDOWS and "/snap/" in low:
        return "snap (read-only squashfs)"
    return None


def expand_roots(roots):
    """Expand install roots into a flat list of patchable app.asar paths."""
    asars = []
    for r in roots:
        low = os.path.basename(r).lower()
        if os.path.isfile(r) and low.endswith(".asar"):
            asars.append(r)
        elif os.path.isfile(r) and low.endswith(".appimage"):
            asars += appimage_extract(r)
        elif os.path.isdir(r):
            asars += asars_under_dir(r)
        else:
            log("SKIP (not found / unreadable): %s" % r)
    out = []
    for p in _dedupe(asars):
        why = _unsupported_reason(p)
        if why:
            log("SKIP (%s): %s" % (why, p))
        else:
            out.append(p)
    return out


# ------------------------------------------------------------- permissions

def is_admin():
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def writable(path):
    try:
        f = open(path, "r+b")
    except OSError:
        return False
    f.close()
    return True


# ---------------------------------------------------------------- elevation

def _launch_elevated(argv):
    """Platform launcher.  True = child launched and completed; False =
    elevation declined/failed (nothing was changed)."""
    if IS_WINDOWS:
        # Start-Process single-string ArgumentList: double-quote every
        # element; embedded double quotes cannot be represented safely.
        if any('"' in a for a in argv[1:]):
            log("cannot elevate: an argument contains a double quote")
            return False
        arglist = " ".join('"%s"' % a for a in argv[1:])
        ps = ("try { Start-Process -FilePath \"%s\" -ArgumentList '%s' "
              "-Verb RunAs -Wait } catch { exit 5 }" % (argv[0], arglist))
        rc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy",
                             "Bypass", "-Command", ps]).returncode
        if rc == 5:
            log("ELEVATION DECLINED -- nothing was changed")
            return False
        return True
    if IS_MAC:
        cmd = " ".join(shlex.quote(a) for a in argv)
        osa = ('do shell script "%s" with administrator privileges'
               % cmd.replace("\\", "\\\\").replace('"', '\\"'))
        rc = subprocess.run(["/usr/bin/osascript", "-e", osa]).returncode
        if rc != 0:
            log("ELEVATION DECLINED/FAILED (rc=%d)" % rc)
            return False
        return True
    launcher = "sudo"
    if not (sys.stdin and sys.stdin.isatty()) and shutil.which("pkexec"):
        launcher = "pkexec"
    rc = subprocess.run([launcher] + argv).returncode
    if rc != 0:
        log("ELEVATION DECLINED/FAILED (rc=%d)" % rc)
        return False
    return True


def run_elevated(extra_args):
    """Relaunch this script elevated with extra_args (the child is by
    definition the elevated instance and receives --marker/--childlog).
    Returns (ok, results) taken from the child's marker.  Marker and
    child log live at mkstemp-random paths owned by the invoking user --
    a multi-user /tmp offers no predictable root-write target."""
    fd, marker = tempfile.mkstemp(prefix="zcode-defence-", suffix=".marker")
    os.close(fd)
    fd, childlog = tempfile.mkstemp(prefix="zcode-defence-", suffix=".childlog")
    os.close(fd)
    argv = [sys.executable, os.path.abspath(sys.argv[0]),
            "--elevated", "--marker", marker, "--childlog", childlog] + extra_args
    log("requesting elevation (%s) ..."
        % ("UAC" if IS_WINDOWS else "osascript" if IS_MAC else "sudo/pkexec"))
    launched = _launch_elevated(argv)
    # relay the child's log (its stdout may be invisible on Windows)
    try:
        with open(childlog, "r", encoding="utf-8", errors="replace") as f:
            for line in f.readlines()[-30:]:
                print("  " + line.rstrip())
    except OSError:
        pass
    results, ok = [], False
    if launched:
        try:
            with open(marker, "r", encoding="utf-8") as f:
                text = f.read().strip()
            try:
                payload = json.loads(text)
                ok, results = payload.get("ok", False), payload.get("results", [])
            except ValueError:  # plain-text fallback marker
                ok, results = text.startswith("OK"), []
            if not ok:
                log("elevated run reported failure")
            elif not results:
                log("WARNING: marker carried no per-target results "
                    "(plain-text fallback); state not merged")
        except OSError:
            log("elevated run finished but wrote no/invalid marker")
    for p in (marker, childlog):
        try:
            os.remove(p)
        except OSError:
            pass
    return ok, results


# ----------------------------------------------------------------- version

_VERSION_CACHE = {}


def _asar_header_layout(mm):
    """(json_off, json_len, data_start) for the archive header, or None.

    Official asar writes two concatenated pickles: [4][headerBufLen]
    [payloadLen][jsonLen][json...].  Some third-party repackers emit the
    simplified single-pickle form [payloadLen][jsonLen][json...].  Accept
    both; the official form is tried first."""
    if len(mm) < 16:
        return None
    a, b, c, d = struct.unpack("<4I", mm[0:16])
    for json_off, json_len, data_start in ((16, d, 8 + b), (8, b, 4 + a)):
        if (0 < json_len and json_off + json_len <= len(mm)
                and data_start <= len(mm)
                and mm[json_off] == 0x7B                 # '{'
                and mm[json_off + json_len - 1] == 0x7D):  # '}'
            return json_off, json_len, data_start
    return None


def asar_version(path):
    """The app's version from package.json inside the archive, or None.

    Locates the header JSON via the asar pickle layout (see
    _asar_header_layout), then reads the top-level package.json exactly,
    so a bundled dependency's version can never be mistaken for the
    app's.  None just
    means "nonstandard / undeterminable"; callers treat it as
    informational, never fatal."""
    if path in _VERSION_CACHE:
        return _VERSION_CACHE[path]
    ver = None
    import mmap as _m
    f = None
    mm = None
    try:
        f = open(path, "rb")
        mm = _m.mmap(f.fileno(), 0, access=_m.ACCESS_READ)
        lay = _asar_header_layout(mm)
        if lay is None:
            raise ValueError("unrecognized asar header layout")
        json_off, json_len, data_start = lay
        header = json.loads(bytes(mm[json_off:json_off + json_len])
                            .decode("utf-8", "replace"))
        entry = ((header or {}).get("files") or {}).get("package.json") or {}
        size, off = int(entry.get("size", 0)), int(entry.get("offset", -1))
        if size > 0 and off >= 0 and data_start + off + size <= len(mm):
            pkg = json.loads(bytes(mm[data_start + off:
                                      data_start + off + size])
                             .decode("utf-8", "replace"))
            ver = pkg.get("version") or None
    except (OSError, ValueError, KeyError, TypeError, struct.error):
        ver = None
    finally:
        if mm is not None:
            mm.close()
        if f is not None:
            f.close()
    _VERSION_CACHE[path] = ver
    return ver


def note_target_version(path):
    """Log the detected app version for a target and warn when it differs
    from the version recorded at its last patch/restore -- that means the
    app updated and replaced app.asar (state offsets are then stale, but
    unused: both directions re-derive from context).  Returns the current
    version (possibly None)."""
    cur = asar_version(path)
    prev = state_version(path)
    if cur is None:
        log("app version: UNKNOWN (nonstandard asar layout?) -- continuing")
    elif prev and prev != cur:
        log("app version: %s  (state recorded %s -- app updated since last "
            "run; stale state offsets are unused)" % (cur, prev))
    else:
        log("app version: %s" % cur)
    return cur


# ------------------------------------------------------------ core editing

def scan(mm):
    """Locate gate sites.  Returns {"unpatched": [...], "patched": [...]}
    with absolute operand offsets, deduplicated."""
    out = {"unpatched": [], "patched": []}
    seen = set()
    pos = mm.find(ANCHOR)
    while pos != -1:
        ws = max(0, pos - 64)
        window = mm[ws:pos + 512]
        for m in TARGET_UNPATCHED.finditer(window):
            if m.group(1) != m.group(2):
                log("WARNING: assignment var %r != gate var %r, skipped"
                    % (m.group(1), m.group(2)))
                continue
            off = ws + m.start(2) - 1  # the '!' before the var
            if off not in seen:
                seen.add(off)
                out["unpatched"].append(
                    {"off": off, "var": m.group(2).decode("ascii"),
                     "old": b"!" + m.group(2), "new": truthy_form(m.group(2))}
                )
        for m in TARGET_PATCHED.finditer(window):
            off = ws + m.start(2)  # start of the (!+)[01] operand
            if off not in seen:
                seen.add(off)
                out["patched"].append(
                    {"off": off, "var": m.group(1).decode("ascii"),
                     "old": m.group(2) + m.group(3), "new": b"!" + m.group(1)}
                )
        pos = mm.find(ANCHOR, pos + 1)
    return out


def scan_file(path):
    """Read-only scan; returns (mm, f) -- caller closes both."""
    f = open(path, "rb")
    import mmap as _m
    mm = _m.mmap(f.fileno(), 0, access=_m.ACCESS_READ)
    return mm, f


def endpoint_count(path):
    """Informational: occurrences of the (untouched) upload endpoint."""
    count = 0
    with open(path, "rb") as f:
        import mmap as _m
        mm = _m.mmap(f.fileno(), 0, access=_m.ACCESS_READ)
        try:
            pos = mm.find(b"snapshot/upload-credential")
            while pos != -1:
                count += 1
                pos = mm.find(b"snapshot/upload-credential", pos + 1)
        finally:
            mm.close()
    return count


def apply_edits(f, mm, edits, size_before):
    """In-place equal-length writes with exact offset verification.

    Pre-write: bytes at the offset must equal the expected old value
    (guards against offset drift after an app update).  Post-write: exact
    slice compare -- no neighborhood heuristics that could false-fail on
    unrelated occurrences of short tokens in dense minified code."""
    for e in edits:
        if len(e["old"]) != len(e["new"]):
            raise AssertionError("non equal-length edit refused: %r" % (e,))
        if mm[e["off"]:e["off"] + len(e["old"])] != e["old"]:
            raise AssertionError("pre-write mismatch at offset %d "
                                 "(offset drift?)" % e["off"])
    for e in edits:
        mm[e["off"]:e["off"] + len(e["old"])] = e["new"]
    mm.flush()
    os.fsync(f.fileno())
    if os.path.getsize(f.name) != size_before:
        raise AssertionError("asar size changed -- aborting")
    for e in edits:
        if mm[e["off"]:e["off"] + len(e["new"])] != e["new"]:
            raise AssertionError("post-write verify failed at offset %d"
                                 % e["off"])


def edit_file(path, restore=False, dry=False):
    """Patch or restore one asar in place.

    Returns {"status": ..., "edits": [...], "error": ...} with status in:
      patched / restored          -- this run changed the file
      already-patched / original  -- nothing to do
      no-gate                     -- pattern absent (version changed?)
      fail                        -- error (see error)
    """
    try:
        f = open(path, "r+b" if not dry else "rb")
    except OSError as exc:
        if not dry:
            return {"status": "fail", "edits": [],
                    "error": "cannot open for write: %r" % (exc,)}
        try:
            f = open(path, "rb")
        except OSError as exc2:
            return {"status": "fail", "edits": [], "error": repr(exc2)}
    import mmap as _m
    size = os.path.getsize(path)
    mm = None
    try:
        mm = _m.mmap(f.fileno(), 0,
                     access=_m.ACCESS_WRITE if not dry else _m.ACCESS_READ)
        sites = scan(mm)
        total = len(sites["patched"]) + len(sites["unpatched"])
        if restore:
            cur, other, done = sites["patched"], sites["unpatched"], "restored"
            idle = "original"
        else:
            cur, other, done = sites["unpatched"], sites["patched"], "patched"
            idle = "already-patched"
        if not cur:
            if other:
                return {"status": idle, "edits": []}
            return {"status": "no-gate", "edits": []}
        edits = cur
        if dry:
            return {"status": "would-%s" % ("restore" if restore else "patch"),
                    "edits": edits}
        note = ", ".join("%s -> %s @%d"
                         % (e["old"].decode(), e["new"].decode(), e["off"])
                         for e in edits)
        apply_edits(f, mm, edits, size)
        after = scan(mm)
        # tolerate mixed starting states: patch converts unpatched ->
        # patched (already-patched stay patched); restore does the reverse
        if restore:
            ok = len(after["patched"]) == 0 and len(after["unpatched"]) == total
        else:
            ok = len(after["patched"]) == total and len(after["unpatched"]) == 0
        if not ok:
            raise AssertionError("post-edit rescan mismatch: %r" % (after,))
        log("%s %s: %d edit(s) [%s], size unchanged (%d bytes)"
            % ("restore" if restore else "patch", path, len(edits), note, size))
        return {"status": done, "edits": edits}
    except Exception as exc:
        log("FATAL on %s: %r" % (path, exc))
        return {"status": "fail", "edits": [], "error": repr(exc)}
    finally:
        if mm is not None:
            mm.close()
        f.close()


def verify_summary(path):
    mm, f = scan_file(path)
    try:
        sites = scan(mm)
    finally:
        mm.close()
        f.close()
    return {"path": path,
            "version": asar_version(path),
            "patched": len(sites["patched"]),
            "unpatched": len(sites["unpatched"]),
            "size": os.path.getsize(path),
            "endpoints": endpoint_count(path)}


# ------------------------------------------------------------- mac signing

def codesign_darwin(asar_path):
    """Re-sign the containing .app with an ad-hoc identity after editing.

    Modifying anything inside a .app invalidates its code signature; on
    Apple Silicon the app will be killed on launch without a valid one.
    Ad-hoc signing needs no developer account."""
    if not IS_MAC:
        return True
    idx = asar_path.find("/Contents/Resources/")
    if idx == -1:
        return True
    app = asar_path[:idx]
    cs = shutil.which("codesign")
    if not cs:
        log("codesign not found -- skipping re-sign of %s" % app)
        return False
    r = subprocess.run([cs, "--force", "--deep", "--sign", "-", app],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if r.returncode == 0:
        log("re-signed (ad-hoc): %s" % app)
        return True
    log("codesign FAILED for %s: %s" % (app, (r.stderr or "").strip()))
    return False


# -------------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    # migrate the old single-target format
    if "targets" not in st:
        st = {"targets": {st["asar"]: st} if st.get("asar") else {}}
    return st


def save_state_target(path, edits, mode, version=None):
    st = load_state() or {"targets": {}}
    st["targets"][path] = {
        "mode": mode,
        "app_version": version,
        "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "edits": [_edit_json(e) for e in edits],
    }
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
    except OSError as exc:
        log("WARNING: state not saved (%r) -- restore still works via "
            "the context regex" % (exc,))
    _chown_sudo(STATE_PATH)


def state_version(path):
    """Version recorded for this target at its last patch/restore, if any."""
    st = load_state()
    if not st:
        return None
    return (st.get("targets") or {}).get(path, {}).get("app_version")
