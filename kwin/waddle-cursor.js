// Waddle cursor KWin script. Loaded at runtime by waddle-desktop only while the pointer
// position is needed, and unloaded afterwards: loading and unloading IS the on/off switch,
// because a running script cannot receive calls.
//
// It reports the pointer position to the host over callDBus as Cursor {x, y}: once at load,
// then at most 10 times per second while the pointer moves (the first move at once, the last
// position of a burst when the 100 ms window ends). It reads nothing else and changes nothing.
//
// Engine is QJSEngine: plain ES2015, no modules. Every optional API is typeof-guarded.
(function () {
    "use strict";

    var SERVICE = "io.github.jacobblomq.Waddle";
    var PATH = "/io/github/jacobblomq/Waddle";
    var INTERFACE = "io.github.jacobblomq.Waddle.KWin";
    var INTERVAL_MS = 100;

    if (typeof workspace !== "object" || workspace === null) {
        return;
    }

    var lastText = null; // last payload sent, so a burst that ends where it began adds nothing
    var lastTime = null; // only used without a timer
    var cooling = false;
    var pending = false;

    function report() {
        try {
            var pos = workspace.cursorPos;
            var x = Number(pos.x);
            var y = Number(pos.y);
            if (!isFinite(x) || !isFinite(y)) {
                return false;
            }
            var text = JSON.stringify({ x: x, y: y });
            if (text === lastText) {
                return false;
            }
            lastText = text;
            callDBus(SERVICE, PATH, INTERFACE, "Cursor", text);
            return true;
        } catch (e) {
            return false;
        }
    }

    // Spike S2.2 UNVERIFIED (scripting API): that Plasma 6.6 offers new QTimer() to scripts
    // with interval / singleShot / timeout / start / stop. Without it the rate is still
    // limited, by the clock, but the last position of a burst is not sent.
    var timer = null;
    try {
        if (typeof QTimer !== "undefined") {
            timer = new QTimer();
            timer.interval = INTERVAL_MS;
            timer.singleShot = true;
            timer.timeout.connect(onTimeout);
        }
    } catch (e) {
        timer = null;
    }

    function sendNow() {
        if (report() && timer) {
            cooling = true;
            timer.start();
        }
    }

    function onTimeout() {
        cooling = false;
        if (pending) {
            pending = false;
            sendNow();
        }
    }

    function onMoved() {
        if (timer) {
            if (cooling) {
                pending = true;
            } else {
                sendNow();
            }
            return;
        }
        var now = Date.now();
        if (lastTime === null || now - lastTime >= INTERVAL_MS || now < lastTime) {
            if (report()) {
                lastTime = now;
            }
        }
    }

    onMoved();
    var moved = workspace.cursorPosChanged;
    if (moved && typeof moved.connect === "function") {
        moved.connect(onMoved);
    }
})();
