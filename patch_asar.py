# -*- coding: utf-8 -*-
"""Apply (or verify) the snapshot-gate patch on ZCode -- cross-platform.

Discovers every ZCode installation on this machine (Windows all-users /
per-user NSIS / Squirrel / scoop, macOS .app, Linux /opt & friends,
AppImage) and patches each app.asar found.  Pin a specific install with
--asar (repeatable; accepts an app.asar file, an install directory,
or an AppImage).

Usage:
    python3 patch_asar.py                # discover + patch all copies
    python3 patch_asar.py --scan         # list targets & status, no changes
    python3 patch_asar.py --dry-run      # show planned edits, no changes
    python3 patch_asar.py --selftest     # patch -> restore -> patch on each
                                         #   target (leaves everything PATCHED)
    python3 patch_asar.py --asar <path>  # pin one target (repeatable)

Elevation model: the parent discovers targets in the user's context and
patches the user-writable ones itself; targets that need root/UAC are
handed to an elevated child as absolute --asar paths (the child never
re-discovers -- root's HOME/PATH would see less than the user's) and it
reports back via a JSON marker.  One elevation prompt per invocation.
The edit is equal-length in-place, never touches endpoint strings, and
is idempotent in both directions.
"""
import os
import sys

if sys.version_info[0] < 3:
    _py = r"C:\Windows\py.exe"
    if os.path.exists(_py):
        os.execv(_py, [_py, "-3", os.path.abspath(__file__)] + sys.argv[1:])
    sys.exit("run with Python 3:  python3 " + os.path.basename(__file__))

import argparse

import common


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--asar", action="append", default=[],
                   help="pin target(s): app.asar file / install dir / AppImage")
    p.add_argument("--scan", action="store_true",
                   help="list discovered targets and gate status")
    p.add_argument("--dry-run", action="store_true",
                   help="show planned edits without writing")
    p.add_argument("--selftest", action="store_true",
                   help="patch -> restore -> patch round-trip per target")
    p.add_argument("--elevated", action="store_true",
                   help=argparse.SUPPRESS)  # internal: we are the elevated child
    p.add_argument("--marker", help=argparse.SUPPRESS)
    p.add_argument("--childlog", help=argparse.SUPPRESS)
    return p


def resolve_targets(args):
    roots = args.asar if args.asar else common.discover()
    if not roots:
        common.log("no ZCode installation discovered -- "
                   "pass --asar <path/to/app.asar | install dir | AppImage>")
        return []
    return common.expand_roots(roots)


def run_pass(asars, args):
    """One in-process pass over targets.  Returns per-target results."""
    results = []
    for path in asars:
        res = {"path": path, "edits": [],
               "version": common.note_target_version(path)}
        if args.selftest:
            common.log("=== SELFTEST %s: patch -> restore -> patch ===" % path)
            r1 = common.edit_file(path)
            if r1["status"] not in ("patched", "already-patched"):
                res.update(status=r1["status"], error=r1.get("error"))
                results.append(res)
                continue
            r2 = common.edit_file(path, restore=True)
            if r2["status"] != "restored":
                res.update(status="fail", error="selftest restore phase: %r"
                           % (r2.get("error") or r2["status"]))
                results.append(res)
                continue
            r3 = common.edit_file(path)
            if r3["status"] not in ("patched", "already-patched"):
                res.update(status="fail", error="selftest re-patch phase: %r"
                           % (r3.get("error") or r3["status"]))
                results.append(res)
                continue
            common.log("=== SELFTEST PASSED on %s (final state: PATCHED) ==="
                       % path)
            res.update(status="patched", edits=r3["edits"])
        else:
            r = common.edit_file(path, dry=args.dry_run)
            res.update(status=r["status"], edits=r["edits"],
                       error=r.get("error"))
        if res["status"] in ("patched", "restored") and not args.dry_run:
            if not common.codesign_darwin(path):
                res["codesign"] = "FAILED"  # edit ok, app may not launch
            if not args.elevated:
                # elevated child leaves state to the parent's marker merge
                common.save_state_target(path, res["edits"], res["status"],
                                         version=res.get("version"))
        results.append(res)
    return results


def report(results, args):
    for r in results:
        line = "%-18s %s" % (r["status"], r["path"])
        if not common.results_ok([r]):
            if r.get("codesign") == "FAILED":
                why = "codesign failed (app may not launch)"
            elif r["status"] == "no-gate":
                why = ("gate pattern not found -- re-recon needed "
                       "(app version %s)" % (r.get("version") or "?"))
            else:
                why = r.get("error") or ""
            line += "  !! %s" % why
        common.log(line)
    if not args.dry_run and results:
        common.log("-- independent re-verification --")
        for r in results:
            try:
                v = common.verify_summary(r["path"])
            except Exception as exc:
                common.log("  verify failed for %s: %r" % (r["path"], exc))
                continue
            common.log("  %s: v=%s patched=%d unpatched=%d size=%d "
                       "endpoints=%d"
                       % (v["path"], v.get("version") or "?", v["patched"],
                          v["unpatched"], v["size"], v["endpoints"]))
    return common.results_ok(results)


def main():
    args = build_parser().parse_args()
    if args.marker or args.childlog:
        common.set_child_io(args.marker, args.childlog)
    asars = resolve_targets(args)
    if not asars:
        return 2

    if args.scan:
        for path in asars:
            v = common.verify_summary(path)
            common.log("%s  v=%s  writable=%s  patched=%d unpatched=%d "
                       "endpoints=%d size=%d"
                       % (path, v.get("version") or "?",
                          common.writable(path), v["patched"],
                          v["unpatched"], v["endpoints"], v["size"]))
        return 0

    if args.dry_run:
        # read-only: no elevation, edit_file falls back to read handles
        ok = report(run_pass(asars, args), args)
        return 0 if ok else 1

    # privilege split: parent handles user-writable targets itself, the
    # elevated child receives exactly the rest (absolute paths, no
    # re-discovery under root's HOME/PATH)
    user_targets = [p for p in asars if common.writable(p)]
    root_targets = [p for p in asars if not common.writable(p)]
    child_results, elevated_ok = [], True
    if root_targets and not args.elevated and not common.is_admin():
        passthrough = ["--selftest"] if args.selftest else []
        for p in root_targets:
            passthrough += ["--asar", os.path.abspath(p)]
        elevated_ok, child_results = common.run_elevated(passthrough)
        for r in child_results:
            if r.get("edits"):
                common.save_state_target(r["path"], r["edits"],
                                         r.get("status", "patched"),
                                         version=r.get("version"))
    else:
        user_targets = asars  # direct (admin or elevated child): all here

    parent_results = run_pass(user_targets, args) if user_targets else []
    results = child_results + parent_results
    if args.marker:  # we are the elevated child: report back to the parent
        common.write_marker(results)
    ok = elevated_ok and report(results, args)
    if not ok:
        common.log("run FAILED (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
