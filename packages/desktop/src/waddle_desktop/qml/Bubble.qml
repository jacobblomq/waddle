// Speech bubble. One Text item holds the whole message; collapsing only changes the height of
// the viewport around it, so streamed text never swaps items or re-creates the layout.
import QtQuick

Item {
    id: root

    property var theme: null
    property real motionScale: 1.0
    property string text
    property bool expanded: false
    property int maxLines: 4
    property bool tailRight: true
    property real maxWidth: 40 * s.u // 320 px at 10 pt
    readonly property int expandedLines: 12
    readonly property bool truncated: label.lineCount > Math.max(1, maxLines)
    readonly property string accessibleText: Accessible.name

    signal moreClicked // the owner toggles `expanded`
    signal dismissed

    readonly property real pad: Math.round(s.u * 1.25)
    readonly property real tailSize: Math.round(s.u * 1.5)
    readonly property real tailHeight: Math.round(tailSize * 0.7)
    readonly property real chrome: 2 * s.inset + 2 * pad + closeButton.width
    readonly property real lineHeight: label.lineCount > 0 ? label.contentHeight / label.lineCount : 0

    implicitWidth: Math.min(maxWidth, Math.ceil(label.implicitWidth) + chrome)
    implicitHeight: 2 * s.inset + 2 * pad + view.height + (more.visible ? more.height + s.u * 0.25 : 0)
                    + tailHeight

    Accessible.role: Accessible.StaticText
    Accessible.name: text

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
        x: s.inset
        y: s.inset
        width: root.width - 2 * s.inset
        height: root.height - 2 * s.inset - root.tailHeight
        radius: s.radius * 1.5
        color: s.viewBg
        border.width: 1
        border.color: s.border
    }

    Item { // tail: a rotated square, clipped so only the half below the panel shows
        x: root.tailRight ? panel.x + panel.width - 3 * s.u - width : panel.x + 3 * s.u
        y: panel.y + panel.height - 1
        width: root.tailSize * 1.5
        height: root.tailHeight + 1
        clip: true
        Rectangle {
            width: root.tailSize
            height: root.tailSize
            anchors.horizontalCenter: parent.horizontalCenter
            y: -height / 2
            rotation: 45
            color: s.viewBg
            border.width: 1
            border.color: s.border
        }
    }

    Flickable {
        id: view
        x: panel.x + root.pad
        y: panel.y + root.pad
        width: Math.max(0, root.width - root.chrome)
        height: Math.min(label.contentHeight,
                         root.lineHeight * (root.expanded ? root.expandedLines : Math.max(1, root.maxLines)))
        contentHeight: label.contentHeight
        clip: true
        interactive: root.expanded && contentHeight > height + 1
        boundsBehavior: Flickable.StopAtBounds
        onInteractiveChanged: if (!interactive) contentY = 0

        Text {
            id: label
            width: view.width
            text: root.text
            textFormat: Text.PlainText // model output is data, never markup
            wrapMode: Text.Wrap
            color: s.viewFg
            font.family: s.fontFamily
            font.pointSize: s.fontPointSize
        }
    }

    Rectangle { // scroll position hint
        visible: view.interactive
        x: view.x + view.width + s.u * 0.5
        y: view.y + view.height * view.visibleArea.yPosition
        width: 3
        height: view.height * view.visibleArea.heightRatio
        radius: 1.5
        color: s.mix(s.viewBg, s.viewFg, 0.35)
    }

    WButton {
        id: more
        objectName: "more"
        theme: root.theme
        motionScale: root.motionScale
        flat: true
        visible: root.truncated
        x: view.x - s.u * 0.5
        y: view.y + view.height + s.u * 0.25
        text: root.expanded ? qsTr("less") : qsTr("… more")
        accessibleName: root.expanded ? qsTr("Show less") : qsTr("Show more")
        onClicked: root.moreClicked()
    }

    WButton {
        id: closeButton
        objectName: "close"
        theme: root.theme
        motionScale: root.motionScale
        flat: true
        x: panel.x + panel.width - width - s.u * 0.5
        y: panel.y + s.u * 0.5
        implicitWidth: implicitHeight
        text: "×"
        textColor: s.mix(s.viewBg, s.viewFg, 0.6)
        fontScale: 1.3
        accessibleName: qsTr("Dismiss")
        onClicked: root.dismissed()
    }
}
