// Waddle, the penguin. Drawn entirely from QML shapes in a 160x190 design space and scaled
// uniformly to the item. One `mood` string selects a State; only property animations are used,
// and nothing runs when `running` is false or `motionScale` is 0.
import QtQuick
import QtQuick.Shapes

Item {
    id: root

    // ---- public API -------------------------------------------------------------------
    property string mood: "idle"
    property color accentColor: "#3daee9"
    property bool dark: false
    property real motionScale: 1.0
    property bool running: true
    property real mouthOpen: 0
    property real level: 0
    property real lookX: 0
    property real lookY: 0

    readonly property rect bodyRect: Qt.rect(stage.x + 12 * stage.scale, stage.y + 18 * stage.scale,
                                             136 * stage.scale, 166 * stage.scale)
    // A point (item coordinates) that is always plain scarf, for render tests.
    readonly property point scarfProbe: Qt.point(stage.x + 80 * stage.scale,
                                                 stage.y + 112.5 * stage.scale)
    readonly property bool animating: blinkTimer.running || blinkAnim.running || footTap.running
                                      || pageFlip.running || breathe.running || zFloat.running
                                      || hop.running || knockAnim.running || poseTransition.running

    // Emitted when a one-shot mood (celebrate, alert) has finished playing. With reduced motion
    // or while paused there is nothing to play, so it is emitted on the next event-loop turn.
    signal transientFinished()

    // ---- internals --------------------------------------------------------------------
    readonly property var moods: ({
        "idle": "Waddle is here",
        "listening": "Waddle is listening",
        "watching": "Waddle is watching your screen",
        "thinking": "Waddle is thinking",
        "speaking": "Waddle is speaking",
        "celebrate": "Waddle is celebrating",
        "alert": "Waddle wants your attention",
        "confused": "Waddle is confused",
        "eyes-closed": "Waddle has his eyes closed for privacy",
        "sleeping": "Waddle is sleeping",
        "napping-brain": "Waddle is resting because his brain is offline"
    })
    readonly property string knownMood: moods[mood] !== undefined ? mood : "idle"
    readonly property bool active: running && motionScale > 0
    readonly property real ms: Math.max(0, motionScale)
    readonly property bool eyesOpenMood: ["celebrate", "eyes-closed", "sleeping", "napping-brain"]
                                         .indexOf(knownMood) < 0
    readonly property bool asleep: knownMood === "sleeping" || knownMood === "napping-brain"

    readonly property color bodyColor: dark ? "#43597a" : "#2c3b52"
    readonly property color wingColor: dark ? "#384c6a" : "#243247"
    readonly property color outline: dark ? "#e6edf7" : "#18212f"
    readonly property color ink: "#1d2738"
    readonly property color cream: "#fff7ea"
    readonly property color orange: "#ff9d2e"
    readonly property color orangeDeep: "#e2740f"
    readonly property color scarfShade: Qt.darker(accentColor, 1.35)

    // Animated scalars; every animation writes one of these and resets it when it stops.
    property real blink: 0       // 1 = eyelids fully down
    property real footLift: 0    // idle foot tap
    property real pagePhase: 0   // thinking: 0..1 page turn
    property real breath: 0      // sleeping: 0..1
    property real zPhase: 0.5    // sleeping: 0..1 Zzz drift (0.5 is the still pose)
    property real hopY: 0        // celebrate
    property real knock: 0       // alert: 0..1 flipper swing
    property int blinkCount: 0

    QtObject {
        id: pose
        property real lean: 0        // degrees, whole figure around the feet
        property real flipL: 16      // flipper angles, see PenguinFlipper
        property real flipR: 16
        property real eyeOpen: 1
        property real glow: 0
        property real book: 0
        property real question: 0
        property real zzz: 0
        property real cloud: 0
        property real droop: 0       // sleeping slump
    }

    function playTransient() {
        hop.stop()
        knockAnim.stop()
        if (knownMood !== "celebrate" && knownMood !== "alert")
            return
        if (!active)
            Qt.callLater(root.transientFinished)
        else if (knownMood === "celebrate")
            hop.start()
        else
            knockAnim.start()
    }

    implicitWidth: 160
    implicitHeight: 190
    state: knownMood
    onKnownMoodChanged: playTransient()
    onActiveChanged: {
        if (!active) {
            blinkAnim.stop()
            footTap.stop()
        }
        playTransient()
    }
    Component.onCompleted: playTransient()

    Accessible.role: Accessible.Graphic
    Accessible.name: moods[knownMood]

    states: [
        State { name: "idle" },
        State {
            name: "listening"
            PropertyChanges { pose.lean: 6; pose.glow: 1; pose.flipL: 62 }
        },
        State { name: "watching"; PropertyChanges { pose.flipL: 8; pose.flipR: 8 } },
        State {
            name: "thinking"
            PropertyChanges { pose.book: 1; pose.flipL: -42; pose.flipR: -42 }
        },
        State { name: "speaking"; PropertyChanges { pose.flipL: 34; pose.flipR: 34 } },
        State {
            name: "celebrate"
            PropertyChanges { pose.eyeOpen: 0; pose.flipL: 138; pose.flipR: 138 }
        },
        State { name: "alert"; PropertyChanges { pose.lean: 4; pose.flipR: 150 } },
        State {
            name: "confused"
            PropertyChanges { pose.lean: -9; pose.question: 1; pose.flipR: 40 }
        },
        State { name: "eyes-closed"; PropertyChanges { pose.eyeOpen: 0; pose.flipL: 6; pose.flipR: 6 } },
        State {
            name: "sleeping"
            PropertyChanges { pose.eyeOpen: 0; pose.zzz: 1; pose.droop: 1; pose.lean: 3; pose.flipL: 6; pose.flipR: 6 }
        },
        State {
            name: "napping-brain"
            PropertyChanges { pose.eyeOpen: 0; pose.cloud: 1; pose.droop: 1; pose.lean: 3; pose.flipL: 6; pose.flipR: 6 }
        }
    ]

    transitions: Transition {
        id: poseTransition
        enabled: root.active
        NumberAnimation {
            target: pose
            properties: "lean,flipL,flipR,eyeOpen,glow,book,question,zzz,cloud,droop"
            duration: 240 * root.ms
            easing.type: Easing.InOutQuad
        }
    }

    // ---- animations (all gated on `active`) -------------------------------------------
    Timer {
        id: blinkTimer
        interval: 3600   // sparse on purpose; never below 2500 ms
        repeat: true
        running: root.active && root.eyesOpenMood
        onTriggered: {
            blinkAnim.restart()
            root.blinkCount += 1
            if (root.knownMood === "idle" && root.blinkCount % 3 === 0)
                footTap.restart()
        }
    }
    SequentialAnimation {
        id: blinkAnim
        onStopped: root.blink = 0
        NumberAnimation { target: root; property: "blink"; to: 1; duration: 70 * root.ms }
        NumberAnimation { target: root; property: "blink"; to: 0; duration: 110 * root.ms }
    }
    SequentialAnimation {
        id: footTap
        loops: 2
        onStopped: root.footLift = 0
        NumberAnimation { target: root; property: "footLift"; to: 1; duration: 110 * root.ms }
        NumberAnimation { target: root; property: "footLift"; to: 0; duration: 110 * root.ms }
    }
    SequentialAnimation {
        id: pageFlip
        running: root.active && root.knownMood === "thinking"
        loops: Animation.Infinite
        onStopped: root.pagePhase = 0
        PauseAnimation { duration: 1100 * root.ms }
        NumberAnimation {
            target: root; property: "pagePhase"; from: 0; to: 1
            duration: 520 * root.ms; easing.type: Easing.InOutQuad
        }
        PropertyAction { target: root; property: "pagePhase"; value: 0 }
    }
    SequentialAnimation {
        id: breathe
        running: root.active && root.asleep
        loops: Animation.Infinite
        onStopped: root.breath = 0
        NumberAnimation {
            target: root; property: "breath"; to: 1
            duration: 2200 * root.ms; easing.type: Easing.InOutSine
        }
        NumberAnimation {
            target: root; property: "breath"; to: 0
            duration: 2600 * root.ms; easing.type: Easing.InOutSine
        }
    }
    NumberAnimation {
        id: zFloat
        target: root; property: "zPhase"; from: 0; to: 1
        running: root.active && root.knownMood === "sleeping"
        loops: Animation.Infinite
        duration: 3200 * root.ms
        onStopped: root.zPhase = 0.5
    }
    SequentialAnimation {
        id: hop
        onStopped: root.hopY = 0
        onFinished: root.transientFinished()
        PauseAnimation { duration: 120 * root.ms }
        NumberAnimation { target: root; property: "hopY"; to: 16; duration: 170 * root.ms; easing.type: Easing.OutQuad }
        NumberAnimation { target: root; property: "hopY"; to: 0; duration: 170 * root.ms; easing.type: Easing.InQuad }
        NumberAnimation { target: root; property: "hopY"; to: 7; duration: 110 * root.ms; easing.type: Easing.OutQuad }
        NumberAnimation { target: root; property: "hopY"; to: 0; duration: 110 * root.ms; easing.type: Easing.InQuad }
        PauseAnimation { duration: 500 * root.ms }
    }
    SequentialAnimation {
        id: knockAnim
        onStopped: root.knock = 0
        onFinished: root.transientFinished()
        PauseAnimation { duration: 260 * root.ms }
        SequentialAnimation {
            loops: 2
            NumberAnimation { target: root; property: "knock"; to: 1; duration: 80 * root.ms; easing.type: Easing.InQuad }
            NumberAnimation { target: root; property: "knock"; to: 0; duration: 130 * root.ms; easing.type: Easing.OutQuad }
            PauseAnimation { duration: 90 * root.ms }
        }
        PauseAnimation { duration: 500 * root.ms }
    }

    // ---- drawing ----------------------------------------------------------------------
    Item {
        id: stage
        width: 160
        height: 190
        scale: Math.min(root.width / 160, root.height / 190)
        transformOrigin: Item.TopLeft
        x: (root.width - 160 * scale) / 2
        y: (root.height - 190 * scale) / 2

        Rectangle {   // ground shadow
            x: 80 - width / 2; y: 179; width: 96 - root.hopY * 1.5; height: 10; radius: 5
            color: "#000000"
            opacity: root.dark ? 0.35 : 0.16
        }

        Item {
            id: figure
            width: 160
            height: 190
            transform: Translate { y: -root.hopY }

            // Feet
            Rectangle {
                x: 40; y: 167; width: 36; height: 17; radius: 8.5
                color: root.orange; border.color: root.orangeDeep; border.width: 1.5
                transform: Rotation { origin.x: 30; origin.y: 12; angle: 14 * root.footLift }
            }
            Rectangle {
                x: 84; y: 167; width: 36; height: 17; radius: 8.5
                color: root.orange; border.color: root.orangeDeep; border.width: 1.5
            }

            Item {
                id: torso
                width: 160
                height: 190
                transform: [
                    Scale {
                        origin.x: 80; origin.y: 172
                        yScale: 1 - 0.035 * pose.droop + 0.022 * root.breath
                        xScale: 1 + 0.02 * pose.droop - 0.008 * root.breath
                    },
                    Rotation { origin.x: 80; origin.y: 172; angle: pose.lean }
                ]

                // Listening glow: halos around the body whose strength follows the mic level.
                Shape {
                    id: glow
                    readonly property real lvl: Math.max(0, Math.min(1, root.level))
                    readonly property string egg: "M 80 20 C 114 20 138 62 138 114 C 138 152 114 172 80 172 C 46 172 22 152 22 114 C 22 62 46 20 80 20 Z"
                    anchors.fill: parent
                    visible: pose.glow > 0
                    opacity: pose.glow * (0.35 + 0.65 * lvl)
                    preferredRendererType: Shape.CurveRenderer
                    ShapePath {
                        fillColor: "transparent"
                        strokeColor: Qt.alpha(root.accentColor, 0.35)
                        strokeWidth: 20 + 14 * glow.lvl
                        joinStyle: ShapePath.RoundJoin
                        PathSvg { path: glow.egg }
                    }
                    ShapePath {
                        fillColor: "transparent"
                        strokeColor: Qt.alpha(root.accentColor, 0.8)
                        strokeWidth: 10 + 6 * glow.lvl
                        joinStyle: ShapePath.RoundJoin
                        PathSvg { path: glow.egg }
                    }
                }

                Shape {
                    anchors.fill: parent
                    preferredRendererType: Shape.CurveRenderer
                    ShapePath {   // egg body
                        fillColor: root.bodyColor
                        strokeColor: root.outline
                        strokeWidth: 2
                        PathSvg { path: "M 80 20 C 114 20 138 62 138 114 C 138 152 114 172 80 172 C 46 172 22 152 22 114 C 22 62 46 20 80 20 Z" }
                    }
                    ShapePath {   // belly
                        fillColor: root.cream
                        strokeColor: "transparent"
                        PathSvg { path: "M 80 104 C 104 104 118 118 118 138 C 118 156 102 166 80 166 C 58 166 42 156 42 138 C 42 118 56 104 80 104 Z" }
                    }
                    ShapePath {   // heart-topped face
                        fillColor: root.cream
                        strokeColor: "transparent"
                        PathSvg { path: "M 80 49 C 89 38 114 39 118 62 C 121 84 104 99 80 99 C 56 99 39 84 42 62 C 46 39 71 38 80 49 Z" }
                    }
                }

                // Cheek blush
                Rectangle { x: 45; y: 81; width: 14; height: 8; radius: 4; color: "#ff8fa3"; opacity: 0.6 }
                Rectangle { x: 101; y: 81; width: 14; height: 8; radius: 4; color: "#ff8fa3"; opacity: 0.6 }

                PenguinEye {
                    x: 49; y: 55
                    ink: root.ink
                    openness: pose.eyeOpen * (1 - root.blink)
                    happy: root.knownMood === "celebrate"
                    lookX: root.knownMood === "watching" ? root.lookX : 0
                    lookY: root.knownMood === "watching" ? root.lookY : 0
                }
                PenguinEye {
                    x: 85; y: 55
                    ink: root.ink
                    openness: pose.eyeOpen * (1 - root.blink)
                    happy: root.knownMood === "celebrate"
                    lookX: root.knownMood === "watching" ? root.lookX : 0
                    lookY: root.knownMood === "watching" ? root.lookY : 0
                }

                // Beak: the lower half drops with mouthOpen.
                Item {
                    id: beak
                    readonly property real open: Math.max(0, Math.min(1, root.mouthOpen))
                    x: 68; y: 74; width: 24; height: 26
                    Rectangle {
                        x: 6; y: 8; width: 12; height: 3 + 7 * beak.open; radius: 3
                        color: "#7a2318"
                        visible: beak.open > 0.02
                    }
                    Shape {
                        y: 6.5 * beak.open
                        width: 24; height: 20
                        preferredRendererType: Shape.CurveRenderer
                        ShapePath {
                            fillColor: root.orangeDeep
                            strokeColor: "transparent"
                            PathSvg { path: "M 5 9 Q 12 18 19 9 Z" }
                        }
                    }
                    Shape {
                        width: 24; height: 20
                        preferredRendererType: Shape.CurveRenderer
                        ShapePath {
                            fillColor: root.orange
                            strokeColor: root.orangeDeep
                            strokeWidth: 1
                            joinStyle: ShapePath.RoundJoin
                            PathSvg { path: "M 3 9 Q 12 0 21 9 Q 12 13 3 9 Z" }
                        }
                    }
                }

                // Knitted scarf in the accent colour, with a knot and a tail.
                Rectangle {   // tail
                    x: 99; y: 116; width: 16; height: 34; radius: 3
                    color: root.accentColor; border.color: root.scarfShade; border.width: 1.2
                    rotation: -10
                    transformOrigin: Item.Top
                    Rectangle { x: 1.2; y: 18; width: 13.6; height: 3; color: root.scarfShade; opacity: 0.55 }
                    Rectangle { x: 1.2; y: 24; width: 13.6; height: 3; color: root.scarfShade; opacity: 0.55 }
                    Repeater {
                        model: 4
                        Rectangle {
                            required property int index
                            x: 1.5 + index * 3.6; y: 33; width: 2.2; height: 5; radius: 1
                            color: root.scarfShade
                        }
                    }
                }
                Shape {
                    anchors.fill: parent
                    preferredRendererType: Shape.CurveRenderer
                    ShapePath {
                        fillColor: root.accentColor
                        strokeColor: root.scarfShade
                        strokeWidth: 1.2
                        joinStyle: ShapePath.RoundJoin
                        PathSvg { path: "M 21 96 Q 80 112 139 96 Q 142 105 140 113 Q 80 129 20 113 Q 18 105 21 96 Z" }
                    }
                    ShapePath {   // ribbing near both edges; the middle stays plain
                        fillColor: "transparent"
                        strokeColor: root.scarfShade
                        strokeWidth: 1.2
                        strokeStyle: ShapePath.DashLine
                        dashPattern: [2, 2.5]
                        PathSvg { path: "M 24 101 Q 80 116 136 101 M 23 109 Q 80 125 137 109" }
                    }
                }
                Rectangle {   // knot
                    x: 97; y: 104; width: 18; height: 17; radius: 6
                    color: root.accentColor; border.color: root.scarfShade; border.width: 1.2
                    rotation: -8
                }

                // Thinking: a tiny book with a page that keeps turning.
                Item {
                    id: book
                    x: 56; y: 126; width: 48; height: 30
                    visible: pose.book > 0
                    opacity: pose.book
                    Rectangle { anchors.fill: parent; radius: 3; color: "#b5533c"; border.color: "#7d3626"; border.width: 1 }
                    Rectangle { x: 3; y: 2.5; width: 20.5; height: 24; color: "#fffdf6" }
                    Rectangle { x: 24.5; y: 2.5; width: 20.5; height: 24; color: "#fffdf6" }
                    Repeater {
                        model: 8
                        Rectangle {
                            required property int index
                            x: index < 4 ? 6 : 27.5; y: 7 + (index % 4) * 4.5
                            width: index % 4 === 3 ? 9 : 14.5; height: 1.4
                            color: "#9aa3b2"
                        }
                    }
                    Rectangle {   // the turning page
                        x: 24; y: 2.5; width: 20.5; height: 24
                        color: "#f1ebdc"; border.color: "#cfc6b0"; border.width: 0.8
                        visible: root.pagePhase > 0
                        transform: Scale { xScale: 1 - 2 * root.pagePhase }
                    }
                    Rectangle { x: 23.4; y: 1.5; width: 1.2; height: 26; color: "#7d3626" }
                }

                PenguinFlipper {
                    x: 14; y: 99
                    angle: pose.flipL
                    fill: root.wingColor; outline: root.outline
                }
                PenguinFlipper {
                    x: 120; y: 99
                    mirrored: true
                    angle: pose.flipR - 38 * root.knock
                    fill: root.wingColor; outline: root.outline
                }
            }
        }

        // Alert: little knock marks beside the raised flipper.
        Shape {
            x: 132; y: 44; width: 28; height: 30
            visible: root.knownMood === "alert" && (root.knock > 0 || !root.active)
            opacity: root.active ? root.knock : 1
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                fillColor: "transparent"
                strokeColor: root.outline
                strokeWidth: 2.2
                capStyle: ShapePath.RoundCap
                PathSvg { path: "M 12 6 L 16 2 M 16 14 L 23 13 M 13 22 L 18 26" }
            }
        }

        // Confused: a question mark.
        Shape {
            x: 124; y: 4; width: 30; height: 44
            visible: pose.question > 0
            opacity: pose.question
            rotation: 12
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                fillColor: "transparent"
                strokeColor: root.outline
                strokeWidth: 8
                capStyle: ShapePath.RoundCap
                PathSvg { path: "M 7 13 C 7 3 23 3 23 13 C 23 20 15 20 15 27 M 15 37 L 15 37.2" }
            }
            ShapePath {
                fillColor: "transparent"
                strokeColor: root.accentColor
                strokeWidth: 4.6
                capStyle: ShapePath.RoundCap
                PathSvg { path: "M 7 13 C 7 3 23 3 23 13 C 23 20 15 20 15 27 M 15 37 L 15 37.2" }
            }
        }

        // Sleeping: three Z's drifting up.
        Item {
            id: zzz
            x: 110; y: 2 + 5 - 10 * root.zPhase; width: 50; height: 50
            visible: pose.zzz > 0
            opacity: pose.zzz * (1 - Math.abs(2 * root.zPhase - 1) * 0.85)
            Repeater {
                model: [{ "x": 2, "y": 30, "s": 0.7 }, { "x": 14, "y": 15, "s": 1.0 }, { "x": 30, "y": 0, "s": 1.35 }]
                Shape {
                    id: zShape
                    required property var modelData
                    x: modelData.x; y: modelData.y; width: 14; height: 16
                    scale: modelData.s
                    transformOrigin: Item.TopLeft
                    preferredRendererType: Shape.CurveRenderer
                    ShapePath {
                        fillColor: "transparent"
                        strokeColor: root.outline
                        strokeWidth: 4.6
                        capStyle: ShapePath.RoundCap
                        joinStyle: ShapePath.RoundJoin
                        PathSvg { path: "M 3 3 L 11 3 L 3 12 L 11 12" }
                    }
                    ShapePath {
                        fillColor: "transparent"
                        strokeColor: "#ffffff"
                        strokeWidth: 2.2
                        capStyle: ShapePath.RoundCap
                        joinStyle: ShapePath.RoundJoin
                        PathSvg { path: "M 3 3 L 11 3 L 3 12 L 11 12" }
                    }
                }
            }
        }

        // Napping brain: a cloud whose cord has come unplugged.
        Shape {
            x: 108; y: 2; width: 52; height: 58
            visible: pose.cloud > 0
            opacity: pose.cloud
            preferredRendererType: Shape.CurveRenderer
            ShapePath {   // cloud
                fillColor: "#f4f7fb"
                strokeColor: root.outline
                strokeWidth: 1.8
                joinStyle: ShapePath.RoundJoin
                PathSvg { path: "M 13 26 C 5 26 3 15 12 14 C 12 5 25 3 29 10 C 36 5 46 11 42 18 C 49 19 48 26 41 26 Z" }
            }
            ShapePath {   // cord stub from the cloud, and the dangling plug's cord
                fillColor: "transparent"
                strokeColor: root.outline
                strokeWidth: 2
                capStyle: ShapePath.RoundCap
                PathSvg { path: "M 27 26 L 27 31 M 27 47 C 27 55 18 55 14 51 M 24.5 37 L 24.5 40 M 29.5 37 L 29.5 40" }
            }
            ShapePath {   // plug body
                fillColor: root.accentColor
                strokeColor: root.outline
                strokeWidth: 1.5
                joinStyle: ShapePath.RoundJoin
                PathSvg { path: "M 22 40 L 32 40 L 32 44 Q 32 47 27 47 Q 22 47 22 44 Z" }
            }
        }
    }
}
