// The text-input window. Unlike the penguin window it accepts keyboard focus; it exists only
// while you are typing. Enter sends, Esc closes (Esc is local to this window, never global).
import QtQuick
import QtQuick.Controls.Basic

Rectangle {
    id: root
    property var theme
    signal submitted(string text)
    signal cancelled()
    readonly property string accessibleText: field.Accessible.name

    width: 460
    height: 56
    radius: 12
    color: theme ? theme.viewBg : "#ffffff"
    border.width: 1
    border.color: theme ? Qt.tint(theme.viewBg, Qt.alpha(theme.viewFg, 0.25)) : "#cccccc"

    function focusInput() { field.forceActiveFocus() }

    TextField {
        id: field
        objectName: "askField"
        anchors.fill: parent
        anchors.margins: 8
        focus: true
        placeholderText: qsTr("Ask Waddle…")
        maximumLength: 4000
        color: root.theme ? root.theme.viewFg : "#000000"
        placeholderTextColor: root.theme ? Qt.alpha(root.theme.viewFg, 0.5) : "#888888"
        selectionColor: root.theme ? root.theme.highlight : "#3daee9"
        selectedTextColor: root.theme ? root.theme.highlightedFg : "#ffffff"
        font.family: root.theme ? root.theme.fontFamily : "sans-serif"
        font.pointSize: root.theme ? root.theme.fontPointSize + 2 : 12
        background: Item {}
        Accessible.name: qsTr("Ask Waddle a question")
        onAccepted: {
            var t = text.trim()
            text = ""
            if (t.length > 0) root.submitted(t); else root.cancelled()
        }
        Keys.onEscapePressed: { text = ""; root.cancelled() }
    }
}
