"""The Unraid template (unraid/deedoop.xml) stays in line with the app and its safe defaults."""

import re
import struct
import xml.etree.ElementTree as ET  # noqa: S405 - parses only our own template file from the repo
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TEMPLATE = ROOT / "unraid" / "deedoop.xml"
RAW = "https://raw.githubusercontent.com/SteveRodrigue/deedoop/main/"


@pytest.fixture(scope="module")
def tpl():
    return ET.parse(TEMPLATE).getroot()  # noqa: S314 - our own repo file, not user input


def _configs(tpl, kind):
    return {c.get("Target"): c for c in tpl.iter("Config") if c.get("Type") == kind}


def test_mnt_is_read_only_by_default(tpl):
    # Changes need both a read-write /mnt and "Allow changes" in Setup; the template never grants the first.
    mnt = _configs(tpl, "Path")["/mnt"]
    assert mnt.get("Mode").split(",")[0] == "ro"
    assert mnt.get("Mode") == "ro,slave"
    assert _configs(tpl, "Path")["/config"].get("Mode") == "rw"


def test_container_is_locked_down(tpl):
    params = tpl.findtext("ExtraParams").split()
    for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert flag in params
    assert params[params.index("--user") + 1] == "99:100"
    assert tpl.findtext("Privileged") == "false"


def test_variables_are_ones_the_app_reads(tpl):
    read = set(re.findall(r'env\("([A-Z_]+)"', (ROOT / "deedoop" / "config.py").read_text()))
    for target in _configs(tpl, "Variable"):
        assert target.startswith("DEEDOOP_")
        assert target.removeprefix("DEEDOOP_") in read, target


def test_admin_password_is_masked(tpl):
    assert _configs(tpl, "Variable")["DEEDOOP_ADMIN_PASSWORD"].get("Mask") == "true"


def test_port_matches_the_image(tpl):
    port = _configs(tpl, "Port")["8080"]
    assert port.get("Default") == "8080"
    assert "EXPOSE 8080" in (ROOT / "Dockerfile").read_text()
    assert "[PORT:8080]" in tpl.findtext("WebUI")


def test_icon_is_a_512px_png_in_this_repo(tpl):
    icon = tpl.findtext("Icon")
    assert icon.startswith(RAW)
    path = ROOT / icon.removeprefix(RAW)
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (512, 512)


def test_readme_link_points_at_the_install_guide(tpl):
    readme = tpl.findtext("ReadMe")
    assert readme.endswith("/docs/install-unraid.md")
    assert (ROOT / "docs" / "install-unraid.md").is_file()


def test_install_guide_names_the_token_type_scope_and_expiry():
    guide = (ROOT / "docs" / "install-unraid.md").read_text()
    token = guide[guide.index("## 1.") : guide.index("## 2.")]
    assert "Personal access token (classic)" in token
    assert "fine-grained" in token
    assert "`read:packages`" in token
    assert "Expiration" in token
    assert "--password-stdin" in guide
