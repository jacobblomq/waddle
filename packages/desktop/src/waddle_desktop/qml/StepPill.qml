// Collapsed guide: one elided line such as "2/5 · Open the Material tab".
import QtQuick

Item {
    id: root

    property var theme: null
    property real motionScale: 1.0
    property string text
    property real maxWidth: 28 * s.u
    readonly property bool truncated: label.truncated
    readonly property string accessibleText: Accessible.name

    signal clicked

    implicitWidth: Math.min(maxWidth, Math.ceil(label.implicitWidth) + 2.5 * s.u + dot.width)
    implicitHeight: label.implicitHeight + s.u
    activeFocusOnTab: true

    Accessible.role: Accessible.Button
    Accessible.name: text
    Accessible.focusable: true
    Accessible.onPressAction: root.clicked()

    Keys.onPressed: event => {
        if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            event.accepted = true
            root.clicked()
        }
    }

    WStyle {
        id: s
        theme: root.theme
        motionScale: root.motionScale
    }

    Rectangle {
        anchors.fill: parent
        radius: height / 2
        color: s.mix(s.windowBg, s.accent, mouse.pressed ? 0.34 : mouse.containsMouse ? 0.24 : 0.14)
        border.width: root.activeFocus ? 2 : 1
        border.color: root.activeFocus ? s.accent : s.mix(s.windowBg, s.accent, 0.6)
        Behavior on color {
            enabled: s.motionScale > 0
            ColorAnimation { duration: s.ms(120) }
        }
    }

    Rectangle {
        id: dot
        x: s.u
        anchors.verticalCenter: parent.verticalCenter
        width: Math.round(s.u * 0.75)
        height: width
        radius: width / 2
        color: s.accent
    }

    Text {
        id: label
        anchors.left: dot.right
        anchors.leftMargin: s.u * 0.5
        anchors.right: parent.right
        anchors.rightMargin: s.u
        anchors.verticalCenter: parent.verticalCenter
        text: root.text
        textFormat: Text.PlainText
        elide: Text.ElideRight
        maximumLineCount: 1
        color: s.windowFg
        font.family: s.fontFamily
        font.pointSize: s.fontPointSize
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
