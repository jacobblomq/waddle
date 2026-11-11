// Node harness that fakes the KWin scripting globals for tests.
//
//   node kwin_mock.js < scenario.json
//
// Scenario: {
//   script: "/path/to/script.js",
//   windows: [{id, resourceClass, caption, frameGeometry: {x,y,width,height}, output}],
//   screens: [{name, geometry}], activeScreen: "name" | null,
//   omit: ["screensChanged", "activeScreen", "window.outputChanged", ...],
//   events: [{type: "geometry", id, frameGeometry} | {type: "output", id, output}
//            | {type: "caption", id, caption} | {type: "add", window} | {type: "remove", id}
//            | {type: "screens", screens}]
// }
// Events are applied the way KWin would (the property changes, then the signal fires); a
// "geometry" event without frameGeometry fires the signal with nothing changed.
//
// Prints {error, calls: [{service, path, interface, method, args}], writes: [{id, prop}],
// windows: [{id, resourceClass, caption, frameGeometry, output}]}. `writes` lists every
// property assignment the SCRIPT made on any window, so tests can assert "never modified".
"use strict";

const fs = require("fs");
const vm = require("vm");

function makeSignal() {
    const handlers = [];
    return {
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
const proxies = {}; // id -> the object scripts see

function makeOutput(screen) {
    return { name: screen.name, geometry: Object.assign({}, screen.geometry) };
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
    };
    ["frameGeometryChanged", "outputChanged", "captionChanged"].forEach(function (name) {
        if (omit.indexOf("window." + name) === -1) { state[name] = makeSignal(); }
    });
    const proxy = new Proxy(state, {
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
    proxies[spec.id] = proxy;
    return proxy;
}

function stateOf(id) {
    return states.filter(function (s) { return s.id === id; })[0].state;
}

const workspace = {
    windowList() { return states.map(function (s) { return proxies[s.id]; }); },
    get screens() { return screens; },
    get activeScreen() { return findOutput(scenario.activeScreen); },
    get activeWindow() { return null; },
    windowAdded: makeSignal(),
    windowRemoved: makeSignal(),
    windowActivated: makeSignal(),
    screensChanged: makeSignal(),
};
omit.forEach(function (name) {
    if (name.indexOf(".") === -1) { delete workspace[name]; }
});

(scenario.windows || []).forEach(addWindow);

const sandbox = {
    workspace: workspace,
    print() {},
    callDBus(service, path, iface, method) {
        calls.push({
            service: service, path: path, interface: iface, method: method,
            args: Array.prototype.slice.call(arguments, 4),
        });
    },
};

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
            states.splice(index, 1);
            emit(workspace.windowRemoved, proxy);
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
