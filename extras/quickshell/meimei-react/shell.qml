import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Io

ShellRoot {
    id: root
    property string source: ""
    property bool showing: false

    IpcHandler {
        target: "react"
        function play(path: string, seconds: int): void {
            console.log("PLAY " + Date.now() + " " + path)
            root.source = ""
            root.source = "file://" + path
            root.showing = true
            hideTimer.interval = seconds * 1000
            hideTimer.restart()
        }
        function hide(): void { root.showing = false }
    }

    Timer { id: hideTimer; onTriggered: root.showing = false }

    PanelWindow {
        visible: root.showing || img.opacity > 0
        anchors { right: true; bottom: true }
        margins { right: 24; bottom: 24 }
        implicitWidth: 240
        implicitHeight: 240
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.namespace: "meimei-react"
        mask: Region {}

        AnimatedImage {
            id: img
            anchors.fill: parent
            fillMode: Image.PreserveAspectFit
            source: root.source
            playing: true
            asynchronous: true
            cache: false
            sourceSize.width: 480
            sourceSize.height: 480
            onStatusChanged: console.log("STATUS " + Date.now() + " " + status)
            opacity: root.showing ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 250 } }
        }
    }
}
