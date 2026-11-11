// A keyboard key: rounded face sitting on a darker bottom edge.
import QtQuick

Item {
    id: root

    property var theme: null
    property real motionScale: 1.0
    property string text
    readonly property string accessibleText: Accessible.name

    implicitWidth: Math.max(label.implicitWidth + s.u * 1.25, implicitHeight)
    implicitHeight: label.implicitHeight + Math.round(s.u * 0.25) + edge
    readonly property int edge: 2

    Accessible.role: Accessible.StaticText
    Accessible.name: text

    WStyle {
        id: s
        theme: root.theme
    }

    Rectangle { // bottom edge
        anchors.fill: parent
        radius: Math.round(s.u * 0.5)
        color: s.mix(s.buttonBg, s.buttonFg, s.dark ? 0.42 : 0.34)
    }
    Rectangle { // face
        anchors.fill: parent
        anchors.bottomMargin: root.edge
        radius: Math.round(s.u * 0.5)
        color: s.mix(s.buttonBg, s.buttonFg, s.dark ? 0.1 : 0.0)
        border.width: 1
        border.color: s.mix(s.buttonBg, s.buttonFg, s.dark ? 0.42 : 0.34)

        Text {
            id: label
            anchors.centerIn: parent
            text: root.text
            textFormat: Text.PlainText
            color: s.buttonFg
            font.family: "monospace"
            font.pointSize: s.fontPointSize * 0.9
            font.weight: Font.DemiBold
        }
    }
}
