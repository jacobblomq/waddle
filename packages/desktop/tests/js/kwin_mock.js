// Node harness that fakes the KWin scripting globals for tests.
//
//   node kwin_mock.js < scenario.json
//
// Scenario: {
//   script: "/path/to/script.js",
//   windows: [{id, resourceClass, caption, frameGeometry: {x,y,width,height}, output,
//              // all optional:
//              resourceName, desktopFileName, pid, clientGeometry, fullScreen, minimized,
//              normalWindow (default true), dialog, popupWindow, transient, transientFor: id,
//              desktops: [n], onAllDesktops, kind: "X11Window" (what String(window) shows)}],
//   screens: [{name, geometry, scale}], activeScreen: "name" | null,
//   active: id | null,            (workspace.activeWindow when the script starts)
//   stack: [id, ...],             (workspace.stackingOrder bottom to top; default: windows)
//   cursor: {x, y}, desktop: n,   (workspace.cursorPos, workspace.currentDesktop.id)
//   omit: ["screensChanged", "activeScreen", "window.outputChanged", "QTimer", ...],
//   events: [{type: "geometry", id, frameGeometry} | {type: "output", id, output}
//            | {type: "caption", id, caption} | {type: "add", window} | {type: "remove", id}
//            | {type: "screens", screens} | {type: "activate", id | null}
//            | {type: "fullscreen", id, fullScreen} | {type: "desktop", desktop}
//            | {type: "cursor", x, y} | {type: "advance", ms}]
// }
// Events are applied the way KWin would (the property changes, then the signal fires); a
// "geometry" event without frameGeometry fires the signal with nothing changed. A removed
// window stays reachable by id, so a test can fire signals on it after it is gone.
//
// Time is virtual: QTimer and Date.now() follow a clock that only "advance" moves, firing
// due timers in order. Every callDBus is stamped with that clock.
//
// Prints {error, calls: [{service, path, interface, method, args, at}], writes: [{id, prop}],
// windows: [{id, resourceClass, caption, frameGeometry, output}],
// connections: {id: n} (handlers currently connected to each window ever created),
// activeTimers: n}. `writes` lists every property assignment the SCRIPT made on any window,
// so tests can assert "never modified". A call of a state-changing method on a window
// (closeWindow, setMaximize, ...) or on workspace (slotWindowClose, raiseWindow, ...) and an
// assignment to a workspace property are recorded there too, as {id, prop: "name()"} and
// {id: "workspace", prop}: the call does nothing, so a script cannot hide it in a try block.
"use strict";

const fs = require("fs");
const vm = require("vm");

function makeSignal() {
    const handlers = [];
    return {
        count() { return handlers.length; },
        connect(fn) { handlers.push(fn); },
        disconnect(fn) {
            const i = handlers.indexOf(fn);
            if (i !== -1) { handlers.splice(i, 1); }
        },
        emit() {
            const args = arguments;
            handlers.slice().forEach(function (fn) { fn.apply(null, args); });
        },
    };
}

const scenario = JSON.parse(fs.readFileSync(0, "utf8"));
const omit = scenario.omit || [];
const calls = [];
const writes = [];
const states = []; // raw window state, mutated by the harness without being recorded
const allStates = {}; // id -> raw state of every window ever created, removed ones included
const proxies = {}; // id -> the object scripts see
const WINDOW_SIGNALS = ["frameGeometryChanged", "outputChanged", "captionChanged",
    "fullScreenChanged", "clientGeometryChanged"];
const OPTIONAL_PROPS = ["resourceName", "desktopFileName", "pid", "fullScreen", "minimized",
    "dialog", "popupWindow", "transient", "onAllDesktops"];

// Names a script has no business calling. They do not exist on the fake objects, so each is
// served as a function that only records the call.
const WINDOW_MUTATORS = /^(set[A-Z]\w*|close\w*|kill\w*|destroy\w*)$/;
const WORKSPACE_MUTATORS = new RegExp("^(slot[A-Z]\\w*|set[A-Z]\\w*|raise\\w*|lower\\w*|" +
    "send\\w*|show[A-Z]\\w*|hide[A-Z]\\w*|create\\w*|remove\\w*|close\\w*|kill\\w*|" +
    "move\\w*|resize\\w*|activate\\w*)$");

let clock = 0;
const timers = [];

function QTimer() {
    this.interval = 0;
    this.singleShot = false;
    this.active = false;
    this.due = 0;
    this.timeout = makeSignal();
    timers.push(this);
}
QTimer.prototype.start = function (ms) {
    if (typeof ms === "number") { this.interval = ms; }
    this.active = true;
    this.due = clock + this.interval;
};
QTimer.prototype.stop = function () { this.active = false; };

function advance(ms) {
    const end = clock + ms;
    for (;;) {
        const due = timers.filter(function (t) { return t.active && t.due <= end; })
            .sort(function (a, b) { return a.due - b.due; })[0];
        if (!due) { break; }
        clock = Math.max(clock, due.due);
        if (due.singleShot) { due.active = false; } else { due.due = clock + due.interval; }
        due.timeout.emit();
    }
    clock = end;
}

function makeOutput(screen) {
    return {
        name: screen.name,
        geometry: Object.assign({}, screen.geometry),
        devicePixelRatio: screen.scale === undefined ? 1 : screen.scale,
    };
}

let screens = (scenario.screens || []).map(makeOutput);

function findOutput(name) {
    return screens.filter(function (s) { return s.name === name; })[0] || null;
}

function addWindow(spec) {
    const state = {
        internalId: "{" + spec.id + "}",
        resourceClass: spec.resourceClass,
        caption: spec.caption,
        frameGeometry: Object.assign({}, spec.frameGeometry),
        output: findOutput(spec.output),
        normalWindow: spec.normalWindow === undefined ? true : spec.normalWindow,
        desktops: (spec.desktops || []).map(function (n) { return desktopOf(n); }),
    };
    OPTIONAL_PROPS.forEach(function (name) {
        if (spec[name] !== undefined) { state[name] = spec[name]; }
    });
    if (spec.clientGeometry) { state.clientGeometry = Object.assign({}, spec.clientGeometry); }
    if (spec.transientFor !== undefined) {
        Object.defineProperty(state, "transientFor", {
            enumerable: true,
            get() { return proxies[spec.transientFor] || null; },
        });
    }
    if (spec.kind) {
        state.toString = function () { return "KWin::" + spec.kind + "(0x1)"; };
    }
    WINDOW_SIGNALS.forEach(function (name) {
        if (omit.indexOf("window." + name) === -1) { state[name] = makeSignal(); }
    });
    const proxy = new Proxy(state, {
        get(target, prop, receiver) {
            if (typeof prop === "string" && !(prop in target) && WINDOW_MUTATORS.test(prop)) {
                return function () { writes.push({ id: spec.id, prop: prop + "()" }); };
            }
            return Reflect.get(target, prop, receiver);
        },
        set(target, prop, value) {
            writes.push({ id: spec.id, prop: String(prop) });
            target[prop] = prop === "frameGeometry" ? Object.assign({}, value) : value;
            return true;
        },
        defineProperty(target, prop, descriptor) {
            writes.push({ id: spec.id, prop: String(prop) });
            return Reflect.defineProperty(target, prop, descriptor);
        },
        deleteProperty(target, prop) {
            writes.push({ id: spec.id, prop: String(prop) });
            return delete target[prop];
        },
    });
    states.push({ id: spec.id, state: state });
    allStates[spec.id] = state;
    proxies[spec.id] = proxy;
    return proxy;
}

function stateOf(id) {
    if (!allStates[id]) { throw new Error("unknown window: " + id); }
    return allStates[id];
}

const desktops = {};
function desktopOf(n) {
    if (!desktops[n]) { desktops[n] = { id: "desktop-" + n }; }
    return desktops[n];
}

let activeId = scenario.active === undefined ? null : scenario.active;
let currentDesktop = desktopOf(scenario.desktop === undefined ? 1 : scenario.desktop);
let cursorPos = Object.assign({ x: 0, y: 0 }, scenario.cursor || {});

const workspace = {
    windowList() { return states.map(function (s) { return proxies[s.id]; }); },
    get screens() { return screens; },
    get activeScreen() { return findOutput(scenario.activeScreen); },
    get activeWindow() { return activeId === null ? null : proxies[activeId] || null; },
    get stackingOrder() {
        const ids = scenario.stack || states.map(function (s) { return s.id; });
        return ids.map(function (id) { return proxies[id]; });
    },
    get cursorPos() { return Object.assign({}, cursorPos); },
    get currentDesktop() { return currentDesktop; },
    windowAdded: makeSignal(),
    windowRemoved: makeSignal(),
    windowActivated: makeSignal(),
    screensChanged: makeSignal(),
    cursorPosChanged: makeSignal(),
    currentDesktopChanged: makeSignal(),
};
omit.forEach(function (name) {
    if (name.indexOf(".") === -1) { delete workspace[name]; }
});

(scenario.windows || []).forEach(addWindow);

const workspaceProxy = new Proxy(workspace, {
    get(target, prop, receiver) {
        if (typeof prop === "string" && !(prop in target) && WORKSPACE_MUTATORS.test(prop)) {
            return function () { writes.push({ id: "workspace", prop: prop + "()" }); };
        }
        return Reflect.get(target, prop, receiver);
    },
    set(target, prop) {
        writes.push({ id: "workspace", prop: String(prop) });
        return true;
    },
    defineProperty(target, prop) {
        writes.push({ id: "workspace", prop: String(prop) });
        return true;
    },
    deleteProperty(target, prop) {
        writes.push({ id: "workspace", prop: String(prop) });
        return true;
    },
});

const sandbox = {
    workspace: workspaceProxy,
    print() {},
    callDBus(service, path, iface, method) {
        calls.push({
            service: service, path: path, interface: iface, method: method,
            args: Array.prototype.slice.call(arguments, 4),
            at: clock,
        });
    },
    Date: { now() { return clock; } },
};
if (omit.indexOf("QTimer") === -1) { sandbox.QTimer = QTimer; }

function emit(signal) {
    if (signal) { signal.emit.apply(signal, Array.prototype.slice.call(arguments, 1)); }
}

let error = null;
try {
    vm.runInNewContext(fs.readFileSync(scenario.script, "utf8"), sandbox,
        { filename: scenario.script });
    (scenario.events || []).forEach(function (event) {
        if (event.type === "add") {
            emit(workspace.windowAdded, addWindow(event.window));
        } else if (event.type === "remove") {
            const proxy = proxies[event.id];
            const index = states.map(function (s) { return s.id; }).indexOf(event.id);
            // "listed": true keeps the window in windowList() while the signal runs, in
            // case KWin removes it from the list only afterwards.
            if (!event.listed) { states.splice(index, 1); }
            if (activeId === event.id) { activeId = null; }
            emit(workspace.windowRemoved, proxy);
            if (event.listed) { states.splice(index, 1); }
        } else if (event.type === "screens") {
            screens = event.screens.map(makeOutput);
            emit(workspace.screensChanged);
        } else if (event.type === "geometry") {
            const state = stateOf(event.id);
            if (event.frameGeometry) { state.frameGeometry = Object.assign({}, event.frameGeometry); }
            emit(state.frameGeometryChanged);
        } else if (event.type === "output") {
            const state = stateOf(event.id);
            state.output = findOutput(event.output);
            emit(state.outputChanged);
        } else if (event.type === "caption") {
            const state = stateOf(event.id);
            state.caption = event.caption;
            emit(state.captionChanged);
        } else if (event.type === "activate") {
            activeId = event.id;
            emit(workspace.windowActivated, event.id === null ? null : proxies[event.id]);
        } else if (event.type === "fullscreen") {
            const state = stateOf(event.id);
            state.fullScreen = event.fullScreen;
            emit(state.fullScreenChanged);
        } else if (event.type === "desktop") {
            currentDesktop = desktopOf(event.desktop);
            emit(workspace.currentDesktopChanged);
        } else if (event.type === "cursor") {
            cursorPos = { x: event.x, y: event.y };
            emit(workspace.cursorPosChanged);
        } else if (event.type === "advance") {
            advance(event.ms);
        } else {
            throw new Error("unknown event type: " + event.type);
        }
    });
} catch (e) {
    error = String(e && e.stack ? e.stack : e);
}

process.stdout.write(JSON.stringify({
    error: error,
    calls: calls,
    writes: writes,
    connections: Object.keys(allStates).reduce(function (out, id) {
        out[id] = WINDOW_SIGNALS.reduce(function (n, name) {
            return n + (allStates[id][name] ? allStates[id][name].count() : 0);
        }, 0);
        return out;
    }, {}),
    activeTimers: timers.filter(function (t) { return t.active; }).length,
    windows: states.map(function (s) {
        return {
            id: s.id,
            resourceClass: s.state.resourceClass,
            caption: s.state.caption,
            frameGeometry: s.state.frameGeometry,
            output: s.state.output ? s.state.output.name : null,
        };
    }),
}));
