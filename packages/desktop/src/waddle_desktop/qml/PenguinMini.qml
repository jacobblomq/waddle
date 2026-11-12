// Waddle, minimal style: a small flat penguin icon. Same public API as Penguin.qml.
// Moods are shown with the smallest possible change: eyes, a tilt, or one tiny glyph.
import QtQuick
import QtQuick.Shapes

Item {
    id: root

    property string mood: "idle"
    property color accentColor: "#3daee9"
    property bool dark: false
    property real motionScale: 1.0
    property bool running: true
    property real mouthOpen: 0
    property real level: 0
    property real lookX: 0
    property real lookY: 0

    signal transientFinished()

    implicitWidth: 72
    implicitHeight: 80

    // ---- derived state ---------------------------------------------------------------
    readonly property var names: ({
        "idle": qsTr("Waddle is idle"),
        "listening": qsTr("Waddle is listening"),
        "watching": qsTr("Waddle is watching"),
        "thinking": qsTr("Waddle is thinking"),
        "speaking": qsTr("Waddle is speaking"),
        "celebrate": qsTr("Waddle is celebrating"),
        "alert": qsTr("Waddle wants your attention"),
        "confused": qsTr("Waddle is confused"),
        "eyes-closed": qsTr("Waddle has his eyes closed for privacy"),
        "sleeping": qsTr("Waddle is asleep"),
        "napping-brain": qsTr("Waddle is napping: his brain is unreachable")
    })
    readonly property string knownMood: names[mood] !== undefined ? mood : "idle"
    readonly property bool active: running && motionScale > 0
    readonly property real k: motionScale > 0 ? motionScale : 1
    readonly property bool asleep: knownMood === "sleeping" || knownMood === "napping-brain"
    readonly property bool eyesShut: asleep || knownMood === "eyes-closed"
    readonly property bool oneShot: knownMood === "celebrate" || knownMood === "alert"

    readonly property real s: Math.min(width / 72, height / 80)
    readonly property real ox: (width - 72 * s) / 2
    readonly property real oy: (height - 80 * s) / 2
    readonly property rect bodyRect: Qt.rect(ox + 9 * s, oy + 11 * s, 54 * s, 66 * s)
    // A point that shows accentColor while listening (used by tests).
    readonly property point accentProbe: Qt.point(ox + 36 * s, oy + 8.2 * s)
    readonly property bool animating: blinkTimer.running || blink.running || breathe.running
                                      || dots.running || hop.running || shake.running

    readonly property string accessibleText: Accessible.name
    Accessible.role: Accessible.Graphic
    Accessible.name: names[knownMood]

    // ---- animated values -------------------------------------------------------------
    property real eyeOpen: 1      // 1 open, ~0 shut (blink)
    property real bob: 0          // 0..1 slow breathing while asleep
    property real phase: 0        // 0..3 thinking dots
    property real hopY: 0
    property real wobble: 0
    readonly property real beakDrop: knownMood === "speaking" ? 3.2 * Math.max(0, Math.min(1, mouthOpen)) : 0

    Timer {
        id: blinkTimer
        interval: 4200
        repeat: true
        running: root.active && !root.eyesShut
        onTriggered: blink.restart()
    }
    SequentialAnimation {
        id: blink
        NumberAnimation { target: root; property: "eyeOpen"; to: 0.1; duration: 70 * root.k }
        NumberAnimation { target: root; property: "eyeOpen"; to: 1; duration: 90 * root.k }
    }
    SequentialAnimation {
        id: breathe
        loops: Animation.Infinite
        running: root.active && root.asleep
        NumberAnimation { target: root; property: "bob"; from: 0; to: 1; duration: 1800 * root.k; easing.type: Easing.InOutSine }
        NumberAnimation { target: root; property: "bob"; from: 1; to: 0; duration: 1800 * root.k; easing.type: Easing.InOutSine }
    }
    NumberAnimation {
        id: dots
        target: root; property: "phase"
        from: 0; to: 3; duration: 1200 * root.k
        loops: Animation.Infinite
        running: root.active && root.knownMood === "thinking"
    }
    SequentialAnimation {
        id: hop
        NumberAnimation { target: root; property: "hopY"; to: -9; duration: 160 * root.k; easing.type: Easing.OutQuad }
        NumberAnimation { target: root; property: "hopY"; to: 0; duration: 220 * root.k; easing.type: Easing.OutBounce }
        onFinished: if (root.knownMood === "celebrate") root.transientFinished()
    }
    SequentialAnimation {
        id: shake
        loops: 2
        NumberAnimation { target: root; property: "wobble"; to: 7; duration: 70 * root.k }
        NumberAnimation { target: root; property: "wobble"; to: -7; duration: 120 * root.k }
        NumberAnimation { target: root; property: "wobble"; to: 0; duration: 70 * root.k }
        onFinished: if (root.knownMood === "alert") root.transientFinished()
    }

    function settle() {
        blink.stop(); hop.stop(); shake.stop()
        eyeOpen = 1; hopY = 0; wobble = 0
        if (knownMood !== "sleeping" && knownMood !== "napping-brain") bob = 0
        if (knownMood !== "thinking") phase = 0
    }
    // Read knownMood directly: a dependent binding such as oneShot may not have been
    // re-evaluated yet when the change handler runs.
    function isOneShot() { return knownMood === "celebrate" || knownMood === "alert" }
    function finishIfStill() {
        if (isOneShot() && !(running && motionScale > 0)) root.transientFinished()
    }
    function play() {
        settle()
        if (!isOneShot()) return
        if (!(running && motionScale > 0)) { Qt.callLater(finishIfStill); return }
        if (knownMood === "celebrate") hop.restart(); else shake.restart()
    }
    onKnownMoodChanged: play()
    onActiveChanged: play()

    // ---- the drawing (72 x 80 design space) --------------------------------------------
    Item {
        id: art
        x: root.ox
        y: root.oy + root.hopY * root.s
        width: 72
        height: 80
        scale: root.s
        transformOrigin: Item.TopLeft
        opacity: root.knownMood === "napping-brain" ? 0.55 : 1

        // listening ring (the one place the accent color shows)
        Rectangle {
            x: 5; y: 7; width: 62; height: 70; radius: 31
            color: "transparent"
            border.width: 2.4
            border.color: root.accentColor
            visible: root.knownMood === "listening"
            opacity: 0.45 + 0.55 * Math.max(0, Math.min(1, root.level))
        }

        Item {
            id: figure
            width: 72; height: 80
            rotation: root.wobble + (root.knownMood === "confused" ? 9 : 0)
            transformOrigin: Item.Bottom
            y: root.asleep ? root.bob * 0.8 : 0

            // feet
            Rectangle { x: 22; y: 70; width: 11; height: 6; radius: 3; color: "#f5a623" }
            Rectangle { x: 39; y: 70; width: 11; height: 6; radius: 3; color: "#f5a623" }
            // body
            Rectangle {
                x: 10; y: 12; width: 52; height: 61; radius: 26
                color: root.dark ? "#3b4358" : "#2b3040"
                border.width: root.dark ? 1 : 0
                border.color: "#7f8aa3"
            }
            // belly
            Rectangle { x: 19; y: 32; width: 34; height: 38; radius: 17; color: "#f7f5ee" }
            // eyes: dots, or short lines when shut
            Repeater {
                model: [27, 45]
                Rectangle {
                    required property int modelData
                    readonly property real open: root.eyesShut ? 0 : root.eyeOpen
                    width: root.eyesShut ? 7 : 5.4
                    height: Math.max(1.4, 5.4 * open)
                    radius: height / 2
                    color: "#ffffff"
                    x: modelData - width / 2 + (root.knownMood === "watching" ? root.lookX * 2 : 0)
                    y: 25 - height / 2 + (root.knownMood === "watching" ? root.lookY * 1.5 : 0)
                }
            }
            // beak: opens while speaking
            Shape {
                ShapePath {
                    strokeWidth: 0; strokeColor: "transparent"; fillColor: "#f5a623"
                    startX: 31.5; startY: 30
                    PathLine { x: 40.5; y: 30 }
                    PathLine { x: 36; y: 34.5 }
                    PathLine { x: 31.5; y: 30 }
                }
                ShapePath {
                    strokeWidth: 0; strokeColor: "transparent"; fillColor: "#d98a12"
                    startX: 33; startY: 31.5 + root.beakDrop
                    PathLine { x: 39; y: 31.5 + root.beakDrop }
                    PathLine { x: 36; y: 34.5 + root.beakDrop }
                    PathLine { x: 33; y: 31.5 + root.beakDrop }
                }
            }
        }

        // one tiny glyph beside the head, only when a mood needs it
        Row {
            x: 26; y: 1; spacing: 3
            visible: root.knownMood === "thinking"
            Repeater {
                model: 3
                Rectangle {
                    required property int index
                    width: 5; height: 5; radius: 2.5
                    color: root.accentColor
                    opacity: root.active ? (Math.floor(root.phase) === index ? 1 : 0.3) : 0.8
                }
            }
        }
        Text {
            x: 52; y: -1 - (root.asleep ? root.bob * 2 : 0)
            visible: text.length > 0
            text: root.asleep ? "z" : root.knownMood === "confused" ? "?" : root.knownMood === "alert" ? "!" : ""
            color: root.knownMood === "alert" ? "#f67400" : (root.dark ? "#d7dbe6" : "#4a5166")
            opacity: root.asleep ? 0.55 + 0.45 * root.bob : 1
            font.pixelSize: 15
            font.bold: true
        }
        // napping-brain: a small hollow "offline" dot, so it differs from plain sleep
        Rectangle {
            x: 6; y: 4; width: 9; height: 9; radius: 4.5
            visible: root.knownMood === "napping-brain"
            color: "transparent"
            border.width: 2
            border.color: "#f67400"
        }
    }
}
