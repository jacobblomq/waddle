// Small themed push button. `flat` gives a borderless link-like variant.
import QtQuick

Item {
    id: root

    property var theme: null
    property real motionScale: 1.0
    property string text
    property bool primary: false
    property bool flat: false
    property string accessibleName: text
    property color textColor: primary ? s.highlightedFg : flat ? s.link : s.buttonFg
    property real fontScale: 1.0
    readonly property string accessibleText: Accessible.name
    readonly property bool hovered: mouse.containsMouse
    readonly property bool down: mouse.pressed

    signal clicked

    implicitWidth: Math.max(label.implicitWidth + (flat ? 1 : 2.5) * s.u, implicitHeight)
    implicitHeight: label.implicitHeight + (flat ? 0.5 : 1) * s.u
    opacity: enabled ? 1 : 0.4
    activeFocusOnTab: true

    Accessible.role: Accessible.Button
    Accessible.name: accessibleName
    Accessible.focusable: true
    Accessible.onPressAction: if (enabled) root.clicked()

    Keys.onPressed: event => {
        if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            event.accepted = true
            if (!event.isAutoRepeat) // a held key must not fire the action over and over
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
        radius: Math.round(s.u * 0.6)
        color: {
            if (root.flat)
                return Qt.alpha(s.accent, root.down ? 0.3 : root.hovered ? 0.16 : 0)
            if (root.primary)
                return root.down ? Qt.darker(s.accent, 1.15) : root.hovered ? Qt.lighter(s.accent, 1.1) : s.accent
            return root.down ? s.mix(s.buttonBg, s.accent, 0.3) : s.buttonBg
        }
        border.width: root.activeFocus ? 2 : root.flat ? 0 : 1
        border.color: {
            if (root.activeFocus)
                return root.primary ? s.mix(s.accent, s.windowFg, 0.5) : s.accent
            if (root.primary)
                return Qt.darker(s.accent, 1.2)
            return root.hovered ? s.accent : s.border
        }
        Behavior on color {
            enabled: s.motionScale > 0
            ColorAnimation { duration: s.ms(120) }
        }
    }

    Text {
        id: label
        anchors.centerIn: parent
        width: Math.min(implicitWidth, root.width)
        text: root.text
        textFormat: Text.PlainText
        elide: Text.ElideRight
        color: root.textColor
        font.family: s.fontFamily
        font.pointSize: s.fontPointSize * root.fontScale
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
