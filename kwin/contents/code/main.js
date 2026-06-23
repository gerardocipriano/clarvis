// clarvis active-window tracker (KWin 6 script).
//
// KWin scripts are sandboxed and cannot touch the filesystem, so we push the
// active window's class to the clarvis daemon over the session bus via
// callDBus(). The daemon (zero.gc.clarvis) caches it and uses it to decide
// between the VSCode integrated-terminal branch and the standalone-Konsole
// branch when a double clap fires.

function report(win) {
    var cls = (win && win.resourceClass) ? String(win.resourceClass) : "";
    // Fire-and-forget with a no-op callback so a missing daemon (not yet
    // started / crashed) doesn't spam KWin's log with unhandled-call errors.
    callDBus("zero.gc.clarvis",
             "/zero/gc/clarvis",
             "zero.gc.clarvis",
             "SetActiveWindow",
             cls,
             function () {});
}

// Fire on every focus change, and once now for the currently focused window.
workspace.windowActivated.connect(report);
report(workspace.activeWindow);
