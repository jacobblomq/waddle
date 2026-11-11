"""Names that must agree between the desktop app, the KWin rule and the KWin scripts (D-010)."""

APP_ID = "io.github.jacobblomq.Waddle"
CAPTURE_APP_ID = "io.github.jacobblomq.Waddle.Capture"

# The KWin rule and scripts match on window class (APP_ID) *and* these exact titles, so
# Settings/History/Eyes windows are never affected (ARCHITECTURE §4.2).
PENGUIN_WINDOW_TITLE = "Waddle"
OVERLAY_WINDOW_TITLE = "Waddle Highlight"

DBUS_SERVICE = APP_ID
DBUS_PATH = "/io/github/jacobblomq/Waddle"
DBUS_KWIN_INTERFACE = "io.github.jacobblomq.Waddle.KWin"
