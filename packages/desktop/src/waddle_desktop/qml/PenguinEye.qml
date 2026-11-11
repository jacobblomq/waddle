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
        Shape {
            width: 17
            height: 17
            x: 4.5 + Math.max(-1, Math.min(1, eye.lookX)) * 3
            y: 4.5 + Math.max(-1, Math.min(1, eye.lookY)) * 3
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                fillColor: eye.ink
                strokeColor: "transparent"
                PathSvg { path: "M 0 8.5 A 8.5 8.5 0 1 0 17 8.5 A 8.5 8.5 0 1 0 0 8.5 Z" }
            }
            ShapePath {   // highlights
                fillColor: "#ffffff"
                strokeColor: "transparent"
                PathSvg {
                    path: "M 2.5 5 A 3 3 0 1 0 8.5 5 A 3 3 0 1 0 2.5 5 Z "
                          + "M 10.5 12 A 1.5 1.5 0 1 0 13.5 12 A 1.5 1.5 0 1 0 10.5 12 Z"
                }
            }
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
