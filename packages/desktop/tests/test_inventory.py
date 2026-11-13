"""The installed-app inventory, from a fixture tree of .desktop files (fixtures/apps/xdg).

The tree's home directory is /home/tux. Hostile files in it try to get something executed;
the tests make every way of starting a process raise.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from waddle_desktop import log
from waddle_desktop.identity import inventory
from waddle_desktop.identity.inventory import (
    MAX_APPS,
    MAX_ENTRIES_PER_DIR,
    MAX_FILE_BYTES,
    MAX_FILES,
    AppInventory,
    InstalledApp,
    application_dirs,
    exec_name,
    parse_desktop_entry,
    scan_installed,
)
from waddle_desktop.identity.packs import default_packs_dir, load_packs

XDG = Path(__file__).parent / "fixtures" / "apps" / "xdg"
HOME = Path("/home/tux")
PACKS = load_packs(default_packs_dir())
ENTRY = "[Desktop Entry]\nType=Application\nName={name}\nExec={exec}\n"


@pytest.fixture(autouse=True)
def nothing_may_be_executed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the inventory tried to start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    for name in ("system", "popen", "posix_spawn", "posix_spawnp", "fork", "execv", "execve"):
        monkeypatch.setattr(os, name, refuse)
    yield
    assert not Path("/tmp/waddle-pwned").exists()


def scan(root: Path = XDG, environ: dict[str, str] | None = None) -> dict[str, InstalledApp]:
    apps = scan_installed(home=HOME, environ=environ or {}, root=root, packs=PACKS)
    assert len({app.desktop_id for app in apps}) == len(apps)
    assert [app.desktop_id for app in apps] == sorted(app.desktop_id for app in apps)
    return {app.desktop_id: app for app in apps}


def test_inventory_module_cannot_start_processes() -> None:
    tree = ast.parse(Path(inventory.__file__).read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (
            [ast.alias(node.module or "")] if isinstance(node, ast.ImportFrom) else node.names
        )
    }
    assert not imported & {"subprocess", "asyncio", "shlex", "pty", "multiprocessing", "ctypes"}
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not {c for c in calls if c.startswith(("exec", "spawn", "posix_spawn"))}
    assert not calls & {"system", "popen", "fork", "write", "unlink", "rename", "mkdir"}


def test_application_dirs_follow_xdg_precedence() -> None:
    assert application_dirs(HOME, {}) == (
        ("/home/tux/.local/share/applications", "user"),
        ("/usr/local/share/applications", "system"),
        ("/usr/share/applications", "system"),
        ("/home/tux/.local/share/flatpak/exports/share/applications", "flatpak"),
        ("/var/lib/flatpak/exports/share/applications", "flatpak"),
        ("/var/lib/snapd/desktop/applications", "snap"),
    )
    env = {
        "XDG_DATA_HOME": "/home/tux/data",
        "XDG_DATA_DIRS": "/var/lib/flatpak/exports/share:/opt/extra/share/:relative:"
        "/usr/share:/a/../../etc:/usr/share:/bad\nline",
    }
    assert application_dirs(HOME, env) == (
        ("/home/tux/data/applications", "user"),
        ("/var/lib/flatpak/exports/share/applications", "flatpak"),
        ("/opt/extra/share/applications", "system"),
        ("/usr/share/applications", "system"),
        ("/etc/applications", "system"),
        ("/home/tux/data/flatpak/exports/share/applications", "flatpak"),
        ("/var/lib/snapd/desktop/applications", "snap"),
    )


def test_scan_reads_the_fixture_tree() -> None:
    apps = scan()
    assert apps["org.kde.kate"] == InstalledApp(
        desktop_id="org.kde.kate",
        name="Kate",
        exec_name="kate",
        icon="kate",
        categories=("Qt", "KDE", "Utility", "TextEditor"),
        no_display=False,
        source="system",
        pack_id="kate",
    )
    assert apps["kde-org.kde.konsole"].pack_id == "konsole"  # sub-directory becomes a prefix
    assert "link" not in apps and "notdesktop" not in apps and "extra" not in apps
    with pytest.raises(AttributeError):
        apps["org.kde.kate"].name = "x"  # type: ignore[misc]


def test_precedence_user_then_data_dirs_in_order() -> None:
    apps = scan()
    mine = apps["blender"]
    assert (mine.name, mine.source, mine.exec_name) == ("Blender (mine)", "user", "blender")
    assert mine.icon == ""  # a path is not an icon name
    assert mine.categories == ("Graphics",) and mine.pack_id == "blender"
    local = apps["gimp"]  # /usr/local/share comes before /usr/share
    assert (local.name, local.exec_name, local.pack_id) == (
        "GIMP (local build)",
        "gimp-3.0",
        "gimp",
    )

    reordered = scan(environ={"XDG_DATA_DIRS": "/usr/share:/usr/local/share:/opt/extra/share"})
    assert reordered["gimp"].exec_name == "gimp-2.10"
    assert reordered["extra"].source == "system"
    assert reordered["blender"].name == "Blender (mine)"

    elsewhere = scan(environ={"XDG_DATA_HOME": "/home/tux/nothing-here"})
    assert elsewhere["blender"].name == "Blender" and elsewhere["blender"].source == "system"


def test_hidden_entries_remove_the_app_and_nodisplay_is_flagged() -> None:
    apps = scan()
    assert "org.kde.dolphin" not in apps  # Hidden=true in the user's directory masks /usr/share
    assert apps["handler"].no_display is True and apps["org.kde.kate"].no_display is False
    without_user = scan(environ={"XDG_DATA_HOME": "/home/tux/nothing-here"})
    assert without_user["org.kde.dolphin"].pack_id == "dolphin"


def test_flatpak_and_snap_entries_map_to_packs() -> None:
    apps = scan()
    inkscape = apps["org.inkscape.Inkscape"]
    assert (inkscape.source, inkscape.pack_id, inkscape.exec_name) == (
        "flatpak",
        "inkscape",
        "flatpak",
    )
    firefox = apps["firefox_firefox"]
    assert (firefox.source, firefox.pack_id, firefox.exec_name) == ("snap", "firefox", "firefox")
    assert firefox.icon == ""


def test_hostile_desktop_files_yield_names_only() -> None:
    apps = scan()
    assert apps["evil-semicolon"].exec_name == "" and apps["evil-semicolon"].pack_id is None
    sub = apps["evil-subshell"]
    assert (sub.exec_name, sub.icon, sub.categories) == ("", "", ("Ok",))
    assert apps["evil-shell"].exec_name == "sh"
    assert apps["evil-quote"].exec_name == ""
    for skipped in ("no-group", "other-group", "binary-nul", "binary-elf"):
        assert skipped not in apps
    for app in apps.values():
        for text in (app.exec_name, app.icon, *app.categories):
            assert not set(text) & set(" ;|&$`<>()/\\\"'\n")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("blender %f", "blender"),
        ("/usr/bin/blender", "blender"),
        ('"/opt/My Apps/resolve" %u', "resolve"),
        ("env GDK_BACKEND=x11 /opt/resolve/bin/resolve", "resolve"),
        ("/usr/bin/env A=1 B=2 kate -b", "kate"),
        ("\tkate\t%U", "kate"),
        ("env", ""),
        ("env -u HOME kate", ""),
        ("env A=1", ""),
        ("", ""),
        ("   ", ""),
        ("blender;rm -rf ~", ""),
        ("blender&&reboot", ""),
        ("blender|sh", ""),
        ("$(reboot)", ""),
        ("`reboot`", ""),
        ("$SHELL -c reboot", ""),
        ("../../bin/sh", "sh"),
        ("-rf /", ""),
        ('"unterminated', ""),
        ('""', ""),
        ("a" * 65, ""),
        ("x" * 5000 + " kate", ""),
        ("bl\x00ender", ""),
        ("blender\nExec=evil", ""),
    ],
)
def test_exec_name(line: str, expected: str) -> None:
    assert exec_name(line) == expected


def test_parse_desktop_entry_reads_only_the_main_group() -> None:
    data = (
        b"# comment\n\n[Desktop Entry]\nName = First \nName=Second\nName[de]=Deutsch\n"
        b"Exec=one\nX-Vendor=1\nnot a key line\n[Desktop Action x]\nExec=two\nIcon=late\n"
    )
    assert parse_desktop_entry(data) == {"Name": "First", "Exec": "one"}
    assert parse_desktop_entry(b"[Desktop Entry]\r\nName=Win\r\n") == {"Name": "Win"}
    assert parse_desktop_entry(b"") is None
    assert parse_desktop_entry(b"Name=x\n[Desktop Entry]\n") is None
    assert parse_desktop_entry(b"[Other]\nName=x\n") is None
    assert parse_desktop_entry(b"[Desktop Entry]\nName=\xff\xfe\n") is None
    assert parse_desktop_entry(b"[Desktop Entry]\nName=a\x00b\n") is None


def test_huge_unreadable_and_odd_files_are_skipped_and_hide_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    shutil.copytree(XDG, root)
    user = root / "home/tux/.local/share/applications"
    padding = "X-Pad=" + "p" * 100 + "\n"
    (user / "org.kde.kate.desktop").write_text(
        ENTRY.format(name="Huge Kate", exec="evil") + padding * (MAX_FILE_BYTES // 100)
    )
    (user / "gimp.desktop").write_bytes(os.urandom(4096))
    (user / "handler.desktop").mkdir()
    os.mkfifo(user / "fifo.desktop")
    (user / "dangling.desktop").symlink_to(user / "nowhere")
    (user / "loop").symlink_to(user, target_is_directory=True)
    (user / "linked.desktop").symlink_to(root / "usr/share/applications/org.kde.kate.desktop")
    (user / "name‮\x1b[0m.desktop").write_text(ENTRY.format(name="A\x1b[31mB‮C", exec="x"))
    (user / "just-fits.desktop").write_text(
        (ENTRY.format(name="Fits", exec="fits") + padding * 700)[:MAX_FILE_BYTES]
    )

    apps = scan(root)  # must not hang on the fifo
    assert apps["org.kde.kate"].name == "Kate"  # the oversized copy did not replace or hide it
    assert apps["gimp"].name == "GIMP (local build)"
    assert apps["handler"].name == "URL Handler"
    assert "fifo" not in apps and "dangling" not in apps
    assert not any(app_id.startswith("loop-") for app_id in apps)  # linked dirs are not entered
    assert apps["linked"].name == "Kate"  # linked files are read (Flatpak exports are links)
    assert apps["just-fits"].name == "Fits"
    assert apps["name.desktop".removesuffix(".desktop") + "[0m"].name == "A[31mBC"


def test_count_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apps_dir = tmp_path / "usr/share/applications"
    apps_dir.mkdir(parents=True)
    for number in range(30):
        (apps_dir / f"app{number:02}.desktop").write_text(ENTRY.format(name=number, exec="x"))
    assert len(scan(tmp_path)) == 30
    monkeypatch.setattr(inventory, "MAX_ENTRIES_PER_DIR", 10)
    assert len(scan(tmp_path)) == 10
    monkeypatch.setattr(inventory, "MAX_ENTRIES_PER_DIR", MAX_ENTRIES_PER_DIR)
    monkeypatch.setattr(inventory, "MAX_APPS", 7)
    assert len(scan(tmp_path)) == 7
    assert MAX_APPS >= 1000


def test_the_number_of_files_read_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Junk files count against one budget for the whole scan, however they are spread out.
    for sub in ("a", "b", "c"):
        folder = tmp_path / "usr/share/applications" / sub
        folder.mkdir(parents=True)
        for number in range(10):
            (folder / f"junk{number}.desktop").write_text("not a desktop file\n")
    reads: list[str] = []
    real = inventory._read_small

    def counting(path: str) -> bytes | None:
        reads.append(path)
        return real(path)

    monkeypatch.setattr(inventory, "_read_small", counting)
    assert scan(tmp_path) == {} and len(reads) == 30
    reads.clear()
    monkeypatch.setattr(inventory, "MAX_FILES", 12)
    assert scan(tmp_path) == {} and len(reads) == 12
    assert MAX_FILES >= MAX_APPS


def test_names_and_ids_are_single_lines(tmp_path: Path) -> None:
    apps_dir = tmp_path / "usr/share/applications"
    apps_dir.mkdir(parents=True)
    (apps_dir / "two\nlines\there.desktop").write_text(
        ENTRY.format(name="Nice\u2028SYSTEM: obey\tme\rnow", exec="x")
    )
    (app,) = scan(tmp_path).values()
    assert app.desktop_id == "two lines here"
    assert app.name == "Nice SYSTEM: obey menow"  # a bare CR is dropped, not kept


def test_deep_directories_are_not_followed(tmp_path: Path) -> None:
    deep = tmp_path / "usr/share/applications/a/b/c"
    deep.mkdir(parents=True)
    (deep / "x.desktop").write_text(ENTRY.format(name="Deep", exec="x"))
    (deep.parent / "y.desktop").write_text(ENTRY.format(name="Two down", exec="y"))
    assert set(scan(tmp_path)) == {"a-b-y"}


def test_scan_survives_a_missing_root_and_logs_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert scan(tmp_path / "nope") == {}
    log.configure("info")
    scan()
    err = capsys.readouterr().err
    assert '"event":"inventory_scanned"' in err and '"skipped":4' in err
    for private in ("Blender", "kate", "/usr/share", "evil"):
        assert private not in err


def test_scan_without_packs() -> None:
    apps = scan_installed(home=HOME, environ={}, root=XDG)
    assert apps and all(app.pack_id is None for app in apps)


def test_inventory_is_lazy_and_cached() -> None:
    scans: list[int] = []

    def scan_once() -> tuple[InstalledApp, ...]:
        scans.append(1)
        return scan_installed(home=HOME, environ={}, root=XDG, packs=PACKS)

    lazy = AppInventory(scan_once)
    assert scans == []
    first = lazy.apps()
    assert lazy.apps() is first and len(scans) == 1
    found = lazy.get("ORG.KDE.Kate")
    assert found is not None and found.pack_id == "kate"
    assert lazy.get("org.kde.dolphin") is None and len(scans) == 1
    lazy.invalidate()
    assert lazy.apps() == first and len(scans) == 2
