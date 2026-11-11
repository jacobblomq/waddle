// One eye of the penguin: a big glossy eyeball that squashes shut into a calm or happy arc.
import QtQuick
import QtQuick.Shapes

Item {
    id: eye

    property real openness: 1      // 0 shut .. 1 open
    property bool happy: false     // shut eye arcs upward (^) instead of downward
    property real lookX: 0         // -1..1
    property real lookY: 0
    property color ink: "#1d2738"

    readonly property bool shut: openness <= 0.12

    width: 26
    height: 26

    Item {
        anchors.fill: parent
        visible: !eye.shut
        transform: Scale {
            origin.y: 14
            yScale: Math.max(0.12, Math.min(1, eye.openness))
        }

        Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                fillColor: "#ffffff"
                strokeColor: eye.ink
                strokeWidth: 1.5
                PathSvg { path: "M 1 13 A 12 12 0 1 0 25 13 A 12 12 0 1 0 1 13 Z" }
            }
        }
        Rectangle {
            id: pupil
            width: 17
            height: 17
            radius: 8.5
            color: eye.ink
            x: 4.5 + Math.max(-1, Math.min(1, eye.lookX)) * 3
            y: 4.5 + Math.max(-1, Math.min(1, eye.lookY)) * 3

            Rectangle { x: 2.5; y: 2; width: 6; height: 6; radius: 3; color: "#ffffff" }
            Rectangle { x: 10.5; y: 10.5; width: 3; height: 3; radius: 1.5; color: "#ffffff"; opacity: 0.75 }
        }
    }

    Shape {
        anchors.fill: parent
        visible: eye.shut
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeColor: eye.ink
            strokeWidth: 2.8
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            PathSvg { path: eye.happy ? "M 4 17 Q 13 5 22 17" : "M 4 12 Q 13 21 22 12" }
        }
    }
}
