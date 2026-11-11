// Derived colors and metrics shared by the bubble/card components. One place for every tint.
import QtQuick

QtObject {
    property var theme: null
    property real motionScale: 1.0

    function pick(name, fallback) {
        return theme ? theme[name] : fallback
    }
    function mix(a, b, t) {
        return Qt.rgba(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t, a.b + (b.b - a.b) * t, 1)
    }
    function ms(duration) {
        return Math.max(0, duration * motionScale)
    }

    // Neutral fallbacks are used only when no theme object was supplied.
    readonly property bool dark: pick("isDark", false)
    readonly property color accent: pick("accent", "#808080")
    readonly property color windowBg: pick("windowBg", "#ffffff")
    readonly property color windowFg: pick("windowFg", "#000000")
    readonly property color viewBg: pick("viewBg", "#ffffff")
    readonly property color viewFg: pick("viewFg", "#000000")
    readonly property color buttonBg: pick("buttonBg", "#ffffff")
    readonly property color buttonFg: pick("buttonFg", "#000000")
    readonly property color highlight: pick("highlight", "#808080")
    readonly property color highlightedFg: pick("highlightedFg", "#ffffff")
    readonly property color positive: pick("positive", "#000000")
    readonly property color link: pick("link", "#000000")

    readonly property color muted: mix(windowBg, windowFg, 0.62)
    readonly property color border: mix(windowBg, windowFg, dark ? 0.26 : 0.22)
    readonly property color subtle: mix(windowBg, windowFg, 0.07)
    readonly property color shadow: Qt.rgba(0, 0, 0, dark ? 0.10 : 0.045)

    readonly property string fontFamily: pick("fontFamily", "sans-serif")
    readonly property real fontPointSize: Math.max(6, pick("fontPointSize", 10))
    readonly property real u: Math.round(fontPointSize * 0.8) // grid unit: 8 px at 10 pt
    readonly property real radius: Math.round(u * 0.75)
    readonly property real inset: 8 // room around a panel for its shadow
}
