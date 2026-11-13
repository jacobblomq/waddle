"""Identity packs: the first-party files in knowledge/apps, the schema, and the regex guard."""

from __future__ import annotations

import dataclasses
import os
import threading
import time
import typing
from pathlib import Path

import pytest

from waddle_core.link import WindowContext
from waddle_desktop.identity.cache import UNKNOWN_IDENTITY, AppIdentity
from waddle_desktop.identity.caption import version_from_caption, version_from_probe_output
from waddle_desktop.identity.packs import (
    MAX_CAPTION_CHARS,
    MAX_PACK_BYTES,
    MAX_PATTERN_CHARS,
    MAX_PROBE_CHARS,
    Pack,
    PackIndex,
    UnsafePattern,
    VersionFlag,
    check_pattern,
    default_packs_dir,
    load_packs,
)
from waddle_desktop.sandbox.readonly_exec import VERSION_FLAGS

FIXTURES = Path(__file__).parent / "fixtures" / "apps" / "packs"
EXPECTED = {
    "blender",
    "kdenlive",
    "krita",
    "gimp",
    "obs",
    "davinci-resolve",
    "inkscape",
    "libreoffice",
    "firefox",
    "kate",
    "dolphin",
    "konsole",
    "vscode",
    "vscodium",
}


@pytest.fixture(scope="module")
def index() -> PackIndex:
    return load_packs(default_packs_dir())


def pack(index: PackIndex, pack_id: str) -> Pack:
    found = index.get(pack_id)
    assert found is not None, pack_id
    return found


def identity(**changes: object) -> AppIdentity:
    return dataclasses.replace(UNKNOWN_IDENTITY, **changes)  # type: ignore[arg-type]


def test_every_first_party_pack_loads(index: PackIndex) -> None:
    assert index.errors == ()
    assert {p.id for p in index.packs} == EXPECTED
    for p in index.packs:
        assert p.about_path, p.id
        assert p.match.window_classes and p.match.binaries, p.id


def test_probe_flags_are_exactly_the_sandbox_allowlist() -> None:
    assert set(typing.get_args(VersionFlag)) == set(VERSION_FLAGS)


def test_only_the_confident_packs_probe(index: PackIndex) -> None:
    probing = {p.id for p in index.packs if p.version_probe is not None}
    assert probing == {"blender", "gimp", "inkscape", "firefox"}


# (pack, realistic window title, version it shows)
TITLES: list[tuple[str, str, str | None]] = [
    ("blender", "untitled.blend - Blender 5.3", "5.3"),
    ("blender", "(Unsaved) - Blender 5.3.1", "5.3.1"),
    ("blender", "* shot_010.blend [/home/tux/film/shot_010.blend] - Blender 5.2.4 LTS", "5.2.4"),
    ("blender", "scene.blend - Blender 5.4.0 Alpha", "5.4.0-alpha"),
    ("blender", "render_v2.3.blend - Blender 5.3", "5.3"),
    ("blender", "Blender 4.5 notes.blend [/tmp/Blender 4.5 notes.blend] - Blender 5.3", "5.3"),
    ("blender", "Blender", None),
    ("blender", "Blender [/home/tux/old - Blender 2.79.blend]", None),
    ("blender", "Blender Render", None),
    ("blender", "Blender Preferences", None),
    ("blender", "my file - Blender 5", None),
    ("blender", "notes - Blender 5.3 tips.blend", None),
    ("obs", "OBS 31.0.1 - Profile: Untitled - Scenes: Untitled", "31.0.1"),
    ("obs", "OBS 30.2.3 (64-bit, linux) - Profile: v2.0 stream - Scenes: 1.5 setup", "30.2.3"),
    ("obs", "OBS Studio 32.0.0-beta2 - Profile: Untitled - Scenes: Untitled", "32.0.0-beta"),
    ("obs", "OBS 31.1 - Profile: 9.9.9 - Scenes: Untitled", "31.1"),
    ("obs", "Settings", None),
    ("obs", "Profile: OBS 29.0.0 - Scenes: Untitled", None),
    ("obs", "Windowed Projector (Scene) - OBS 12.3", None),
    ("kdenlive", "holiday_v1.2.kdenlive / HD 1080p 25 fps — Kdenlive", None),
    ("kdenlive", "Untitled / UHD 4K 2160p 29.97 fps — Kdenlive", None),
    ("krita", "[Not Saved] (62.1 MiB) — Krita", None),
    ("krita", "sketch 3.0.kra (112.5 MiB) * — Krita", None),
    (
        "gimp",
        "[Untitled]-1.0 (RGB color 8-bit gamma integer, GIMP built-in sRGB, 1 layer) "
        "1920x1080 \u2013 GIMP",
        None,
    ),
    ("gimp", "*[photo v2.10] (imported)-3.0 (RGB color 8-bit) 4000x3000 \u2013 GIMP", None),
    ("gimp", "GNU Image Manipulation Program", None),
    ("davinci-resolve", "DaVinci Resolve Studio - Wedding 2.1", None),
    ("davinci-resolve", "DaVinci Resolve - Untitled Project", None),
    ("inkscape", "*drawing-1.4.svg - Inkscape", None),
    ("inkscape", "New document 1 - Inkscape", None),
    ("libreoffice", "2024 budget.ods — LibreOffice Calc", None),
    ("libreoffice", "report 25.2 final.odt — LibreOffice Writer", None),
    ("libreoffice", "Untitled 1 — LibreOffice Writer", None),
    ("firefox", "Firefox 144.0 release notes — Mozilla Firefox", None),
    ("firefox", "Mozilla Firefox", None),
    ("kate", "main.py — Kate", None),
    ("kate", "CHANGELOG-24.08.md — Kate", None),
    ("dolphin", "Blender 5.3 — Dolphin", None),
    ("konsole", "~ : blender --version — Konsole", None),
    ("konsole", "tux@box: ~/src/app-2.4.1 — Konsole", None),
    ("vscode", "version.py - waddle 0.3 - Visual Studio Code", None),
    ("vscodium", "release-1.96.md - notes - VSCodium", None),
]


@pytest.mark.parametrize(("pack_id", "title", "expected"), TITLES)
def test_caption_patterns_on_realistic_titles(
    index: PackIndex, pack_id: str, title: str, expected: str | None
) -> None:
    assert version_from_caption(pack(index, pack_id), title) == expected


def test_every_pack_has_titles_in_the_table() -> None:
    assert {pack_id for pack_id, _, _ in TITLES} == EXPECTED


# (pack, what "<binary> <flag>" prints, version)
OUTPUTS: list[tuple[str, str, str | None]] = [
    ("blender", "Blender 5.3.1\n\tbuild date: 2026-10-02\n\tbuild hash: a1b2c3d4e5f6\n", "5.3.1"),
    ("blender", "Blender 5.2.4 LTS\n\tbuild date: 2026-09-01\n", "5.2.4"),
    ("blender", "Blender 5.4.0 Alpha\n", "5.4.0-alpha"),
    ("blender", "Color management: using fallback mode\nBlender 4.5.3 LTS\n", "4.5.3"),
    ("blender", "error while loading shared libraries: libGL.so.1 (needs 1.4)\n", None),
    ("blender", "", None),
    ("gimp", "GNU Image Manipulation Program version 3.0.4\n", "3.0.4"),
    ("gimp", "GNU Image Manipulation Program version 2.10.38\n", "2.10.38"),
    ("gimp", "Cannot open display: \n", None),
    ("inkscape", "Inkscape 1.4 (e7c3feb100, 2024-10-09)\n", "1.4"),
    ("inkscape", "Inkscape 1.3.2 (091e20e, 2023-11-25, custom)\n", "1.3.2"),
    (
        "inkscape",
        "Unable to init server: Could not connect: Connection refused\n"
        "Inkscape 1.4.2 (ebf0e940d0, 2025-05-08)\n    Pango version: 1.56.3\n",
        "1.4.2",
    ),
    ("inkscape", "    Pango version: 1.56.3\n", None),
    ("firefox", "Mozilla Firefox 144.0.2\n", "144.0.2"),
    ("firefox", "Mozilla Firefox 140.4.0esr\n", "140.4.0"),
    ("firefox", "Mozilla Firefox 145.0b3\n", "145.0-beta"),
    ("firefox", "Error: no DISPLAY environment variable specified\n", None),
]


@pytest.mark.parametrize(("pack_id", "output", "expected"), OUTPUTS)
def test_probe_patterns_on_realistic_output(
    index: PackIndex, pack_id: str, output: str, expected: str | None
) -> None:
    assert version_from_probe_output(pack(index, pack_id), output) == expected


def test_probe_patterns_cover_every_probing_pack(index: PackIndex) -> None:
    probing = {p.id for p in index.packs if p.version_probe is not None}
    assert {pack_id for pack_id, _, expected in OUTPUTS if expected} == probing


# ---- lookup ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"app_id": "org.blender.Blender", "exe": "/app/bin/blender"}, "blender"),
        ({"app_id": "BLENDER"}, "blender"),
        ({"app_id": "blender_blender", "exe": "/snap/blender/6100/blender"}, "blender"),
        ({"app_id": "something", "exe": "/opt/blender-5.3/blender", "name": "blender"}, "blender"),
        ({"app_id": "org.kde.kate", "exe": "/usr/bin/kate"}, "kate"),
        ({"app_id": "x", "exe": "/usr/lib/libreoffice/program/soffice.bin"}, "libreoffice"),
        ({"app_id": "x", "exe": "/usr/bin/gimp-2.10", "name": "gimp"}, "gimp"),
        ({"app_id": "resolve", "exe": "/opt/resolve/bin/resolve"}, "davinci-resolve"),
        ({"app_id": "codium", "exe": "/usr/share/codium/codium"}, "vscodium"),
        ({"app_id": "code", "exe": "/usr/share/code/code"}, "vscode"),
        ({"app_id": "org.blender.Blender.extra"}, None),
        ({"app_id": "x", "exe": "/usr/bin/blender-launcher", "name": "blender-launcher"}, None),
        ({}, None),
    ],
)
def test_find_by_identity(index: PackIndex, changes: dict[str, str], expected: str | None) -> None:
    found = index.find(identity(**changes))
    assert (found.id if found else None) == expected


def test_find_by_window(index: PackIndex) -> None:
    found = index.find(WindowContext(resource_class="org.kde.Kdenlive", title="x"))
    assert found is not None and found.id == "kdenlive"
    assert index.find(WindowContext(resource_class="kdenlive2")) is None
    assert index.find(WindowContext(resource_class="")) is None


def test_match_prefers_ids_over_binaries_and_ignores_case(index: PackIndex) -> None:
    assert index.match(window_class="Org.KDE.Kate", binary="blender") is index.get("kate")
    assert index.match(binary="Blender") is index.get("blender")
    assert index.match() is None


# ---- loading: bad files are skipped, never raised ------------------------------------------


def test_fixture_directory_loads_the_good_and_reports_the_bad() -> None:
    loaded = load_packs(FIXTURES)
    assert {p.id for p in loaded.packs} == {"good", "webmail", "browser"}
    errors = {e.file: e.reason for e in loaded.errors}
    assert set(errors) == {
        "nested-quantifier.toml",
        "extra-key.toml",
        "bad-flag.toml",
        "no-group.toml",
        "wrong-name.toml",
        "zz-duplicate.toml",
        "not-toml.toml",
        "binary.toml",
    }
    assert "repeat" in errors["nested-quantifier.toml"]
    assert errors["extra-key.toml"].startswith("run:")
    assert errors["bad-flag.toml"].startswith("version_probe.flag")
    assert "version" in errors["no-group.toml"]
    assert errors["not-toml.toml"] == "not valid TOML"
    assert errors["binary.toml"] == "not UTF-8 text"
    assert errors["zz-duplicate.toml"] == "matches the same name as another"
    for reason in errors.values():
        assert "rm -rf" not in reason  # the offending value is not echoed


def test_missing_directory_and_oversized_file(tmp_path: Path) -> None:
    missing = load_packs(tmp_path / "nope")
    assert missing.packs == () and len(missing.errors) == 1

    (tmp_path / "big.toml").write_text('id = "big"\nname = "Big"\n' + "# pad\n" * MAX_PACK_BYTES)
    (tmp_path / "ok.toml").write_text('id = "ok"\nname = "Ok"\n')
    (tmp_path / "dir.toml").mkdir()
    loaded = load_packs(tmp_path)
    assert [p.id for p in loaded.packs] == ["ok"]
    assert {e.file: e.reason for e in loaded.errors} == {
        "big.toml": "file too large",
        "dir.toml": "cannot be read",
    }


def test_schema_caps() -> None:
    base = {"id": "x", "name": "X"}
    Pack.model_validate(base)
    bad: list[dict[str, object]] = [
        {**base, "id": "Has Space"},
        {**base, "name": "x" * 49},
        {**base, "name": "two\nlines"},
        {**base, "about_path": ["a"] * 7},
        {**base, "browser": "yes"},
        {**base, "match": {"binaries": ["../../bin/sh"]}},
        {**base, "match": {"binaries": ["b"] * 33}},
        {**base, "match": {"window_classes": ["x" * 129]}},
        {**base, "caption_version": {"pattern": "(?P<version>1)" + "a" * MAX_PATTERN_CHARS}},
        {**base, "caption_version": {"pattern": "(?P<version>1)", "flags": "i"}},
        {**base, "web_app": {"title_patterns": []}},
        {**base, "web_app": {"title_patterns": ["(x+)+"]}},
        {**base, "version_probe": {"flag": "-v", "pattern": "(?P<version>1)"}},
    ]
    for data in bad:
        with pytest.raises(ValueError):
            Pack.model_validate(data)


# ---- the regex guard -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a+)+$",
        r"(?:a*)*b",
        r"(?:\d+\.?)+x",
        r"(?:a|aa)+$",
        r"(?:a?){1,30}a{30}",
        r"(?:(?:x+)y){2,}",
        r"\d{1,4}(?:\.\d{1,4}){1,3}",  # harmless, but the rule is blunt: write it out
        r"(a)\1",
        r"(?P<q>a)(?(q)b|c)",
        r".*.*.*.*x",
        r".*.*.*x",
        # No nesting at all, and still exponential: every optional item doubles the ways.
        "a?" * 26 + "a" * 26,
        "a{0,16}" * 12 + "x",
        "(?:a|a)" * 24 + "x",
        "(?:a|ab)(?:c|cd)" * 12 + "x",
        r"[a-",
        r"(" * 150 + r")" * 150,
    ],
)
def test_check_pattern_refuses(pattern: str) -> None:
    with pytest.raises(UnsafePattern):
        check_pattern(pattern, version_group=False)


@pytest.mark.parametrize(
    "pattern",
    [
        r"(?P<v>\d+\.\d+)",  # wrong name
        r"(?P<version>\d+)\.(\d+)",  # a second group
        r"\d+\.\d+",  # no group
    ],
)
def test_version_patterns_need_exactly_the_version_group(pattern: str) -> None:
    check_pattern(pattern, version_group=False)
    with pytest.raises(UnsafePattern):
        check_pattern(pattern, version_group=True)


def test_check_pattern_accepts_plain_patterns() -> None:
    for pattern in (
        r" - Blender (?P<version>\d{1,4}\.\d{1,4}(?:\.\d{1,4})?(?: (?:Alpha|Beta))?)$",
        r"^OBS (?P<version>\d+\.\d+)",
        r".*x(?P<version>\d{1,4})",
        r"(?P<version>\d+\.\d+)",
    ):
        assert check_pattern(pattern, version_group=True).pattern == pattern


PATHOLOGICAL = [
    "a" * 199 + "!",
    "1." * 99 + "!",
    " - Blender " * 18,
    "OBS " + "9" * 196,
    ("Blender 1.2 " * 17)[:199] + "x",
    "‮" + "5.3 " * 49,
]


def test_pathological_titles_are_fast_on_every_pack(index: PackIndex) -> None:
    fixtures = load_packs(FIXTURES)
    started = time.perf_counter()
    for p in (*index.packs, *fixtures.packs):
        for title in PATHOLOGICAL:
            version_from_caption(p, title)
            version_from_probe_output(p, title * 20)
            if p.web_app is not None:
                p.web_app.matches(title)
    assert time.perf_counter() - started < 0.5


def test_patterns_near_the_budget_are_still_quick() -> None:
    # Hand-made worst cases that just fit the budget, on the text that hurts them most.
    cases = [
        ("a?" * 16 + "a" * 16 + "x", MAX_CAPTION_CHARS, "a" * 200),
        ("(?:a|a)" * 16 + "x", MAX_CAPTION_CHARS, "a" * 200),
        (r"\d+\d+\d?x", MAX_CAPTION_CHARS, "1" * 200),
        ("a?" * 12 + "a" * 12 + "x", MAX_PROBE_CHARS, "a" * MAX_PROBE_CHARS),
        ("a+x", MAX_PROBE_CHARS, "a" * MAX_PROBE_CHARS),
    ]
    started = time.perf_counter()
    for pattern, text_chars, text in cases:
        regex = check_pattern(pattern, version_group=False, text_chars=text_chars)
        assert regex.search(text) is None
    assert time.perf_counter() - started < 15.0  # about 2 s here; unguarded these never end


def test_one_more_choice_than_the_budget_is_refused() -> None:
    check_pattern("a?" * 16 + "a" * 16, version_group=False)
    with pytest.raises(UnsafePattern):
        check_pattern("a?" * 17 + "a" * 17, version_group=False)


def test_probe_patterns_are_budgeted_for_the_longer_probe_text() -> None:
    # Fine on a 200-character title, far too slow on 4096 characters of program output.
    pattern = r"(?P<version>\d).*.*x$"
    base = {"id": "x", "name": "X"}
    Pack.model_validate({**base, "caption_version": {"pattern": pattern}})
    with pytest.raises(ValueError):
        Pack.model_validate({**base, "version_probe": {"flag": "-V", "pattern": pattern}})


def test_a_pack_file_that_is_a_pipe_does_not_hang_loading(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe.toml")
    (tmp_path / "ok.toml").write_text('id = "ok"\nname = "Ok"\n')
    done: list[PackIndex] = []
    worker = threading.Thread(target=lambda: done.append(load_packs(tmp_path)), daemon=True)
    worker.start()
    worker.join(5.0)
    assert done, "load_packs is stuck opening the pipe"
    assert [p.id for p in done[0].packs] == ["ok"]
    assert {e.file: e.reason for e in done[0].errors} == {"pipe.toml": "cannot be read"}


# ---- web apps --------------------------------------------------------------------------------


def test_browser_window_with_a_known_web_app_title() -> None:
    packs = load_packs(FIXTURES)
    browser = identity(app_id="firefox", exe="/usr/lib/firefox/firefox")
    mail = packs.find(browser, caption="Inbox (3) - Example Mail — Mozilla Firefox")
    assert mail is not None and mail.id == "webmail"
    other = packs.find(browser, caption="Example Mail is down? — Mozilla Firefox")
    assert other is not None and other.id == "browser"
    assert packs.find(browser) is packs.get("browser")
    window = WindowContext(
        resource_class="firefox", title="Re: hi - Example Mail — Mozilla Firefox"
    )
    assert packs.find(window) is packs.get("webmail")


def test_web_app_titles_only_count_in_a_browser() -> None:
    packs = load_packs(FIXTURES)
    editor = identity(app_id="goodapp", exe="/usr/bin/good")
    found = packs.find(editor, caption="Inbox (3) - Example Mail — Mozilla Firefox")
    assert found is packs.get("good")
    assert packs.find(identity(app_id="unknown"), caption="x - Example Mail — y") is None
