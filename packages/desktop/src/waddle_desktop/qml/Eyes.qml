// "Waddle's eyes": the last picture Waddle took and what he believes he is looking at.
// An ordinary window. It never shows more than the single window Waddle was allowed to see.
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

Rectangle {
    id: root
    property var theme
    property var eyes
    readonly property color fg: theme ? theme.windowFg : "#232629"
    readonly property color dim: Qt.alpha(fg, 0.6)
    readonly property string accessibleText: statusLabel.text

    width: 560
    height: 620
    color: theme ? theme.windowBg : "#eff0f1"

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 10

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 220
            radius: 8
            color: root.theme ? root.theme.viewBg : "#ffffff"
            border.width: 1
            border.color: Qt.alpha(root.fg, 0.2)
            clip: true

            Image {
                id: overview
                objectName: "overview"
                anchors.fill: parent
                anchors.margins: 6
                fillMode: Image.PreserveAspectFit
                cache: false
                asynchronous: false
                visible: root.eyes && root.eyes.hasFrame
                source: visible ? "image://eyes/overview?" + root.eyes.revision : ""
                Accessible.name: qsTr("The last picture Waddle took of the focused window")
            }
            Text {
                id: statusLabel
                objectName: "status"
                anchors.centerIn: parent
                width: parent.width - 40
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                visible: !overview.visible
                color: root.dim
                font.family: root.theme ? root.theme.fontFamily : "sans-serif"
                font.pointSize: root.theme ? root.theme.fontPointSize + 1 : 11
                text: !root.eyes ? ""
                    : root.eyes.eyesClosed ? qsTr("Eyes closed. Nothing is being captured.")
                    : qsTr("No picture yet.")
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            Rectangle {
                Layout.preferredWidth: 132
                Layout.preferredHeight: 132
                radius: 8
                color: root.theme ? root.theme.viewBg : "#ffffff"
                border.width: 1
                border.color: Qt.alpha(root.fg, 0.2)
                clip: true
                Image {
                    objectName: "crop"
                    anchors.fill: parent
                    anchors.margins: 4
                    fillMode: Image.PreserveAspectFit
                    cache: false
                    visible: root.eyes && root.eyes.hasFrame
                    source: visible ? "image://eyes/crop?" + root.eyes.revision : ""
                    Accessible.name: qsTr("The detail crop Waddle took")
                }
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 2
                columnSpacing: 10
                rowSpacing: 3
                Repeater {
                    model: root.eyes ? root.eyes.rows : []
                    Text {
                        required property var modelData
                        Layout.row: index
                        Layout.column: 0
                        required property int index
                        text: modelData.label
                        color: root.dim
                        font.family: root.theme ? root.theme.fontFamily : "sans-serif"
                        font.pointSize: root.theme ? root.theme.fontPointSize : 10
                    }
                }
                Repeater {
                    model: root.eyes ? root.eyes.rows : []
                    Text {
                        required property var modelData
                        required property int index
                        Layout.row: index
                        Layout.column: 1
                        Layout.fillWidth: true
                        text: modelData.value
                        color: root.fg
                        wrapMode: Text.WordWrap
                        font.family: root.theme ? root.theme.fontFamily : "sans-serif"
                        font.pointSize: root.theme ? root.theme.fontPointSize : 10
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: root.dim
                font.family: root.theme ? root.theme.fontFamily : "sans-serif"
                font.pointSize: root.theme ? root.theme.fontPointSize - 1 : 9
                text: qsTr("Kept in memory only while this panel is open. Never saved, never sent.")
            }
            WButton {
                objectName: "lookNow"
                theme: root.theme
                text: qsTr("Look now")
                enabled: root.eyes ? root.eyes.canLook : false
                onClicked: root.eyes.lookNow()
            }
        }
    }
}
