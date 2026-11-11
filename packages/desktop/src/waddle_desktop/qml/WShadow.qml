// Soft drop shadow: a few stacked translucent rounded rects. Place it behind the panel it fills.
import QtQuick

Item {
    id: root

    property color color: Qt.rgba(0, 0, 0, 0.05)
    property real radius: 6

    Repeater {
        model: 5
        Rectangle {
            required property int index
            x: -index - 1
            y: -index + 1
            width: root.width + 2 * (index + 1)
            height: root.height + 2 * (index + 1)
            radius: root.radius + index + 1
            color: root.color
        }
    }
}
