# -*- coding: utf-8 -*-
"""Restore the original gate in ZCode's app.asar (undo patch_asar.py) --
cross-platform, same discovery / elevation-split rules as the patcher.

Usage:
    python3 restore_asar.py                # discover + restore all copies
    python3 restore_asar.py --asar <path>  # pin one target (repeatable)

The original minified variable name is re-derived from the assignment
directly feeding the gate (`let r = await this.tokenProvider(); if(...!r)`),
so restoration is precise even when patch-state.json is missing or stale.
Exits cleanly when there is nothing to restore.

Elevated runs report REAL per-target results through the JSON marker --
a failed restore under UAC/sudo/osascript is never reported as success.
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
    p.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--marker", help=argparse.SUPPRESS)
    p.add_argument("--childlog", help=argparse.SUPPRESS)
    return p


def do_restore(paths, args):
    """Restore each asar; returns per-target results (never fabricated)."""
    results = []
    for path in paths:
        cur_ver = common.note_target_version(path)
        r = common.edit_file(path, restore=True)
        entry = {"path": path, "status": r["status"], "edits": r["edits"],
                 "version": cur_ver, "error": r.get("error")}
        if r["status"] == "restored":
            if not common.codesign_darwin(path):
                entry["codesign"] = "FAILED"  # edit ok, app may not launch
            if not args.elevated:
                common.save_state_target(path, r["edits"], "restored",
                                         version=cur_ver)
        common.log("%-18s %s%s" % (
            r["status"], path,
            "  !! %s" % (r.get("error") or "") if r["status"] == "fail" else ""))
        results.append(entry)
    return results


def main():
    args = build_parser().parse_args()
    if args.marker or args.childlog:
        common.set_child_io(args.marker, args.childlog)
    roots = args.asar if args.asar else common.discover()
    if not roots:
        common.log("no ZCode installation discovered -- "
                   "pass --asar <path/to/app.asar | install dir | AppImage>")
        return 2
    asars = common.expand_roots(roots)
    if not asars:
        return 2

    # same privilege split as the patcher
    user_targets = [p for p in asars if common.writable(p)]
    root_targets = [p for p in asars if not common.writable(p)]
    child_results, elevated_ok = [], True
    if root_targets and not args.elevated and not common.is_admin():
        passthrough = []
        for p in root_targets:
            passthrough += ["--asar", os.path.abspath(p)]
        elevated_ok, child_results = common.run_elevated(passthrough)
        for r in child_results:
            if r.get("edits"):
                common.save_state_target(r["path"], r["edits"], "restored",
                                         version=r.get("version"))
    else:
        user_targets = asars

    parent_results = do_restore(user_targets, args) if user_targets else []
    results = child_results + parent_results
    if args.marker:  # we are the elevated child: real results, no fabricating
        common.write_marker(results)

    if results:
        common.log("-- independent re-verification --")
        for r in results:
            try:
                v = common.verify_summary(r["path"])
            except Exception as exc:
                common.log("  verify failed for %s: %r" % (r["path"], exc))
                continue
            common.log("  %s: v=%s patched=%d unpatched=%d size=%d"
                       % (v["path"], v.get("version") or "?",
                          v["patched"], v["unpatched"], v["size"]))
    ok = elevated_ok and common.results_ok(results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
