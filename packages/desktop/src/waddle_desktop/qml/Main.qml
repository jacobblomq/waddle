// Waddle's main surface: the penguin with his bubble, guide card and step pill.
// The window has a fixed size; everything that is not an interactive item is click-through
// (the input mask is the union of the rectangles reported to bridge.setInputRects).
import QtQuick

Item {
    id: root

    // Design-space layout, scaled by bridge.sizeScale for the penguin only.
    readonly property real s: bridge.sizeScale
    readonly property real penguinW: 160 * s
    readonly property real penguinH: 190 * s
    readonly property real pad: 12
    readonly property real dodgeShift: bridge.dodged ? -(penguinW + pad) : 0

    width: Math.max(400, penguinW + 240)
    height: penguinH + 410

    function publishRects() {
        var rects = []
        function add(item, rect) {
            if (item.visible && item.opacity > 0.01)
                rects.push(item.mapToItem(root, rect.x, rect.y, rect.width, rect.height))
        }
        add(penguin, penguin.bodyRect)
        if (bubble.visible)
            add(bubble, Qt.rect(0, 0, bubble.width, bubble.height))
        if (card.visible)
            add(card, Qt.rect(0, 0, card.width, card.height))
        if (pill.visible)
            add(pill, Qt.rect(0, 0, pill.width, pill.height))
        bridge.setInputRects(rects)
    }
    // Coalesce: many geometry changes in one frame publish once.
    function scheduleRects() { rectTimer.restart() }
    Timer { id: rectTimer; interval: 0; onTriggered: root.publishRects() }
    Component.onCompleted: scheduleRects()

    Penguin {
        id: penguin
        objectName: "penguin"
        width: root.penguinW
        height: root.penguinH
        // Positioned by its target, not its animated x, so the mask never trails the animation.
        readonly property real targetX: root.width - width - root.pad + root.dodgeShift
        x: targetX
        y: root.height - height - root.pad
        opacity: bridge.dodged ? 0.35 : 1.0

        mood: bridge.mood
        accentColor: bridge.theme.accent
        dark: bridge.theme.isDark
        motionScale: bridge.motionScale
        running: bridge.running
        mouthOpen: bridge.mouthOpen
        level: bridge.micLevel
        onTransientFinished: bridge.transientDone()

        Behavior on x {
            enabled: bridge.motionScale > 0
            NumberAnimation { duration: 220 * bridge.motionScale; easing.type: Easing.OutCubic }
        }
        Behavior on opacity {
            enabled: bridge.motionScale > 0
            NumberAnimation { duration: 180 * bridge.motionScale }
        }
        onTargetXChanged: root.scheduleRects()
        onYChanged: root.scheduleRects()
        onBodyRectChanged: root.scheduleRects()

        MouseArea {
            id: bodyArea
            x: penguin.bodyRect.x
            y: penguin.bodyRect.y
            width: penguin.bodyRect.width
            height: penguin.bodyRect.height
            hoverEnabled: true
            acceptedButtons: Qt.LeftButton
            property point pressPos
            property bool dragging: false
            onContainsMouseChanged: bridge.bodyHover(containsMouse)
            onPressed: function (mouse) {
                pressPos = Qt.point(mouse.x, mouse.y)
                dragging = false
                bridge.bodyPress()
            }
            onPositionChanged: function (mouse) {
                if (pressed && !dragging
                        && Math.abs(mouse.x - pressPos.x) + Math.abs(mouse.y - pressPos.y) > 8) {
                    dragging = true
                    bridge.startDrag()   // the compositor takes over the move from here
                }
            }
            onReleased: if (!dragging) bridge.bodyClick()
        }
    }

    Bubble {
        id: bubble
        objectName: "bubble"
        theme: bridge.theme
        motionScale: bridge.motionScale
        text: bridge.bubbleText
        expanded: bridge.bubbleExpanded
        maxLines: bridge.bubbleMaxLines
        visible: bridge.bubbleText.length > 0 && !card.visible
        anchors.right: parent.right
        anchors.rightMargin: root.pad
        anchors.bottom: penguin.top
        anchors.bottomMargin: 4
        onMoreClicked: bridge.toggleBubble()
        onDismissed: bridge.dismissBubble()
        onVisibleChanged: root.scheduleRects()
        onWidthChanged: root.scheduleRects()
        onHeightChanged: root.scheduleRects()
    }

    GuideCard {
        id: card
        objectName: "card"
        theme: bridge.theme
        motionScale: bridge.motionScale
        guide: bridge.guide
        showWhy: bridge.showWhy
        visible: bridge.cardOpen
        width: Math.min(implicitWidth, root.width - 2 * root.pad)
        anchors.right: parent.right
        anchors.rightMargin: root.pad
        anchors.bottom: penguin.top
        anchors.bottomMargin: 4
        onAction: function (name) { bridge.guideAction(name) }
        onSubmitted: function (text) { bridge.submitText(text) }
        onVisibleChanged: root.scheduleRects()
        onWidthChanged: root.scheduleRects()
        onHeightChanged: root.scheduleRects()
        // This window never takes keyboard focus (so clicking Waddle does not pull focus away
        // from the app you are working in). Typing happens in the separate Ask window.
        MouseArea {
            parent: card.inputField
            anchors.fill: parent
            cursorShape: Qt.IBeamCursor
            onClicked: bridge.requestAsk()
        }
    }

    StepPill {
        id: pill
        objectName: "pill"
        theme: bridge.theme
        motionScale: bridge.motionScale
        text: bridge.pillText
        visible: bridge.pillText.length > 0
        width: Math.min(implicitWidth, penguin.x - 2 * root.pad)
        anchors.right: penguin.left
        anchors.rightMargin: 6
        anchors.bottom: parent.bottom
        anchors.bottomMargin: root.pad + 18 * root.s
        onClicked: bridge.openCard()
        onVisibleChanged: root.scheduleRects()
        onWidthChanged: root.scheduleRects()
        onXChanged: root.scheduleRects()
    }
}
