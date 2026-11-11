// Guide card: goal, progress, finished steps, the current instruction with inline keycaps,
// actions and a question field. `guide` is plain data shaped like GuideView.to_qml().
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Shapes

Item {
    id: root

    property var theme: null
    property real motionScale: 1.0
    property var guide: null
    property bool showWhy: false
    readonly property Item inputField: input
    readonly property string accessibleText: Accessible.name

    signal action(string name) // "back", "skip", "done", "show", "why"
    signal submitted(string text)

    readonly property var steps: guide && guide.steps ? guide.steps : []
    readonly property int currentIndex: Math.max(0, Math.min(steps.length - 1, guide ? Number(guide.current) || 0 : 0))
    readonly property var step: steps.length > 0 ? steps[currentIndex] : null
    readonly property var pieces: piecesOf(step)
    readonly property real pad: Math.round(s.u * 1.75)

    // Flatten segments into flowable pieces: one per word, one per key, "+" between keys.
    function piecesOf(step) {
        const out = []
        const segments = step && step.segments ? step.segments : []
        for (let i = 0; i < segments.length; i++) {
            const keys = segments[i].keys || []
            for (let k = 0; k < keys.length; k++) {
                if (k > 0)
                    out.push({ key: false, text: "+" })
                out.push({ key: true, text: String(keys[k]) })
            }
            const words = String(segments[i].text || "").split(/\s+/)
            for (let w = 0; w < words.length; w++) {
                if (words[w].length > 0)
                    out.push({ key: false, text: words[w] })
            }
        }
        return out
    }
    function spoken(pieces) {
        let out = ""
        for (let i = 0; i < pieces.length; i++) {
            const tight = pieces[i].text === "+" || (i > 0 && pieces[i - 1].text === "+")
            out += (i > 0 && !tight ? " " : "") + pieces[i].text
        }
        return out
    }

    visible: step !== null
    implicitWidth: step ? 42 * s.u + 2 * s.inset : 0
    implicitHeight: step ? column.implicitHeight + 2 * pad + 2 * s.inset : 0

    Accessible.role: Accessible.Grouping
    Accessible.name: step ? [guide.goal, guide.progressLabel, step.title, spoken(pieces)].join(". ") : ""

    WStyle {
        id: s
        theme: root.theme
        motionScale: root.motionScale
    }

    WShadow {
        anchors.fill: panel
        radius: panel.radius
        color: s.shadow
    }

    Rectangle {
        id: panel
        objectName: "panel"
        anchors.fill: parent
        anchors.margins: s.inset
        radius: s.radius * 1.5
        color: s.windowBg
        border.width: 1
        border.color: s.border
    }

    ColumnLayout {
        id: column
        x: s.inset + root.pad
        y: s.inset + root.pad
        width: Math.max(0, root.width - 2 * (s.inset + root.pad))
        spacing: s.u

        Text {
            objectName: "goal"
            Layout.fillWidth: true
            text: root.step ? String(root.guide.goal || "") : ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: s.windowFg
            font.family: s.fontFamily
            font.pointSize: s.fontPointSize * 1.15
            font.weight: Font.DemiBold
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: s.u

            Text {
                objectName: "progress"
                text: root.step ? String(root.guide.progressLabel || "") : ""
                textFormat: Text.PlainText
                color: s.muted
                font.family: s.fontFamily
                font.pointSize: s.fontPointSize * 0.9
            }
            Row { // one segment per step
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                spacing: 3
                Repeater {
                    model: root.steps.length
                    Rectangle {
                        required property int index
                        width: Math.max(2, (parent.width - 3 * (root.steps.length - 1)) / root.steps.length)
                        height: 4
                        radius: 2
                        color: index < root.currentIndex ? s.mix(s.windowBg, s.accent, 0.55)
                             : index === root.currentIndex ? s.accent : s.mix(s.windowBg, s.windowFg, 0.14)
                    }
                }
            }
        }

        ColumnLayout { // earlier steps
            Layout.fillWidth: true
            Layout.topMargin: s.u * 0.25
            spacing: s.u * 0.4
            visible: root.currentIndex > 0

            Repeater {
                model: root.currentIndex
                RowLayout {
                    id: earlier
                    required property int index
                    readonly property var entry: root.steps[index] || ({})
                    objectName: entry.done ? "doneStep" : "skippedStep"
                    Layout.fillWidth: true
                    spacing: s.u * 0.75

                    Shape { // check mark for done, dash for skipped
                        Layout.preferredWidth: s.u * 1.5
                        Layout.preferredHeight: s.u * 1.5
                        Layout.alignment: Qt.AlignTop
                        ShapePath {
                            strokeColor: earlier.entry.done ? s.positive : s.muted
                            strokeWidth: Math.max(1.5, s.u * 0.22)
                            fillColor: "transparent"
                            capStyle: ShapePath.RoundCap
                            joinStyle: ShapePath.RoundJoin
                            startX: s.u * 0.25
                            startY: s.u * 0.85
                            PathLine {
                                x: earlier.entry.done ? s.u * 0.6 : s.u * 1.25
                                y: earlier.entry.done ? s.u * 1.2 : s.u * 0.85
                            }
                            PathLine {
                                x: s.u * 1.25
                                y: earlier.entry.done ? s.u * 0.4 : s.u * 0.85
                            }
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: String(earlier.entry.title || "")
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        color: s.muted
                        font.family: s.fontFamily
                        font.pointSize: s.fontPointSize
                        Accessible.role: Accessible.ListItem
                        Accessible.name: (earlier.entry.done ? qsTr("Done: %1") : qsTr("Skipped: %1")).arg(text)
                    }
                }
            }
        }

        Rectangle { // current step
            Layout.fillWidth: true
            Layout.topMargin: s.u * 0.25
            implicitHeight: current.implicitHeight + 2 * s.u
            radius: s.radius
            color: s.mix(s.windowBg, s.viewBg, 0.7)
            border.width: 1
            border.color: s.mix(s.windowBg, s.accent, 0.45)

            ColumnLayout {
                id: current
                x: s.u * 1.25
                y: s.u
                width: parent.width - 2 * x
                spacing: s.u * 0.6

                Text {
                    objectName: "stepTitle"
                    Layout.fillWidth: true
                    text: root.step ? String(root.step.title || "") : ""
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: s.windowFg
                    font.family: s.fontFamily
                    font.pointSize: s.fontPointSize
                    font.weight: Font.DemiBold
                }

                Flow {
                    id: flow
                    objectName: "instruction"
                    Layout.fillWidth: true
                    spacing: Math.round(s.u * 0.4)
                    Accessible.role: Accessible.StaticText
                    Accessible.name: root.spoken(root.pieces)

                    Keycap { // sizes every piece to one row height so words and keys line up
                        id: probe
                        theme: root.theme
                        text: "A"
                        visible: false
                    }
                    Repeater {
                        model: root.pieces
                        Loader {
                            id: piece
                            required property var modelData
                            height: probe.implicitHeight
                            width: item ? Math.min(item.implicitWidth, flow.width) : 0
                            sourceComponent: modelData.key ? keyPiece : wordPiece
                            onLoaded: item.text = modelData.text
                        }
                    }
                }

                Text {
                    objectName: "why"
                    Layout.fillWidth: true
                    visible: root.showWhy && text.length > 0
                    text: root.step ? String(root.step.why || "") : ""
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: s.muted
                    font.family: s.fontFamily
                    font.pointSize: s.fontPointSize * 0.95
                    font.italic: true
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: s.u * 0.5

            WButton {
                objectName: "btnShow"
                theme: root.theme
                motionScale: root.motionScale
                flat: true
                text: qsTr("Show me where")
                onClicked: root.action("show")
            }
            WButton {
                objectName: "btnWhy"
                theme: root.theme
                motionScale: root.motionScale
                flat: true
                text: qsTr("Why?")
                onClicked: root.action("why")
            }
            Item { Layout.fillWidth: true }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: s.u * 0.75

            WButton {
                objectName: "btnBack"
                theme: root.theme
                motionScale: root.motionScale
                enabled: root.currentIndex > 0
                text: qsTr("Back")
                onClicked: root.action("back")
            }
            WButton {
                objectName: "btnSkip"
                theme: root.theme
                motionScale: root.motionScale
                text: qsTr("Skip")
                onClicked: root.action("skip")
            }
            Item { Layout.fillWidth: true }
            WButton {
                objectName: "btnDone"
                theme: root.theme
                motionScale: root.motionScale
                primary: true
                text: qsTr("I did it")
                onClicked: root.action("done")
            }
        }

        TextField {
            id: input
            objectName: "input"
            Layout.fillWidth: true
            Layout.topMargin: s.u * 0.25
            placeholderText: qsTr("Ask Waddle…")
            placeholderTextColor: s.mix(s.viewBg, s.viewFg, 0.5)
            color: s.viewFg
            selectionColor: s.highlight
            selectedTextColor: s.highlightedFg
            font.family: s.fontFamily
            font.pointSize: s.fontPointSize
            leftPadding: s.u
            rightPadding: s.u
            topPadding: s.u * 0.75
            bottomPadding: s.u * 0.75
            Accessible.name: placeholderText
            onAccepted: {
                const typed = text.trim()
                if (typed.length === 0)
                    return
                clear()
                root.submitted(typed)
            }
            background: Rectangle {
                radius: Math.round(s.u * 0.6)
                color: s.viewBg
                border.width: input.activeFocus ? 2 : 1
                border.color: input.activeFocus ? s.accent : s.border
            }
        }
    }

    Component {
        id: keyPiece
        Keycap {
            objectName: "keycap"
            theme: root.theme
        }
    }
    Component {
        id: wordPiece
        Text {
            objectName: "word"
            textFormat: Text.PlainText
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            color: s.windowFg
            font.family: s.fontFamily
            font.pointSize: s.fontPointSize
        }
    }
}
