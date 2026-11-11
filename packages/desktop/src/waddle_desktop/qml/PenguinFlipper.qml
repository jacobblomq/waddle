// One flipper, hanging from its shoulder pivot. angle > 0 swings the tip outward/up,
// angle < 0 swings it across the belly. Set mirrored for the right-hand side.
import QtQuick
import QtQuick.Shapes

Item {
    id: flipper

    property real angle: 0
    property bool mirrored: false
    property color fill: "#243247"
    property color outline: "#1b2535"

    width: 26
    height: 54
    transform: [
        Rotation { origin.x: 13; origin.y: 7; angle: flipper.angle },
        Scale { origin.x: 13; xScale: flipper.mirrored ? -1 : 1 }
    ]

    Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            fillColor: flipper.fill
            strokeColor: flipper.outline
            strokeWidth: 1.5
            joinStyle: ShapePath.RoundJoin
            PathSvg { path: "M 13 3 C 23 9 25 35 16 49 C 12 54 5 51 4 43 C 2 29 5 11 13 3 Z" }
        }
    }
}
