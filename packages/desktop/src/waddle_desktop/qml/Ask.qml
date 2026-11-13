// The text-input window. Unlike the penguin window it accepts keyboard focus; it exists only
// while you are typing. Enter sends, Esc closes (Esc is local to this window, never global).
// With a `hint` it asks one small thing instead (which version an app is): the hint is shown
// above the field as plain text.
import QtQuick
import QtQuick.Controls.Basic

Rectangle {
    id: root
    property var theme
    property string hint // empty: an ordinary question
    property string placeholder: qsTr("Ask Waddle…")
    signal submitted(string text)
    signal cancelled()
    readonly property string accessibleText: field.Accessible.name

    width: 460
    height: 56 + (hintLabel.visible ? hintLabel.height + 4 : 0)
    radius: 12
    color: theme ? theme.viewBg : "#ffffff"
    border.width: 1
    border.color: theme ? Qt.tint(theme.viewBg, Qt.alpha(theme.viewFg, 0.25)) : "#cccccc"

    function focusInput() { field.forceActiveFocus() }
    function clearInput() { field.text = "" }

    Text {
        id: hintLabel
        objectName: "askHint"
        visible: root.hint.length > 0
        x: 16
        y: 12
        width: root.width - 32
        text: root.hint
        textFormat: Text.PlainText // app names are data, never markup
        wrapMode: Text.Wrap
        maximumLineCount: 3
        elide: Text.ElideRight
        color: root.theme ? root.theme.viewFg : "#000000"
        font.family: root.theme ? root.theme.fontFamily : "sans-serif"
        font.pointSize: root.theme ? root.theme.fontPointSize : 10
    }

    TextField {
        id: field
        objectName: "askField"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: 8
        height: 40
        focus: true
        placeholderText: root.placeholder
        maximumLength: 4000
        color: root.theme ? root.theme.viewFg : "#000000"
        placeholderTextColor: root.theme ? Qt.alpha(root.theme.viewFg, 0.5) : "#888888"
        selectionColor: root.theme ? root.theme.highlight : "#3daee9"
        selectedTextColor: root.theme ? root.theme.highlightedFg : "#ffffff"
        font.family: root.theme ? root.theme.fontFamily : "sans-serif"
        font.pointSize: root.theme ? root.theme.fontPointSize + 2 : 12
        background: Item {}
        Accessible.name: root.hint.length > 0 ? root.hint : qsTr("Ask Waddle a question")
        onAccepted: {
            var t = text.trim()
            text = ""
            if (t.length > 0) root.submitted(t); else root.cancelled()
        }
        Keys.onEscapePressed: { text = ""; root.cancelled() }
    }
}
