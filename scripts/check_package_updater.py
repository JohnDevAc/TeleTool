#!/usr/bin/env python3
"""Validate that package-managed Web updates remain wired end to end."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def require(path: str, *needles: str) -> None:
    target = ROOT / path
    if not target.is_file():
        raise SystemExit(f"Missing package updater asset: {path}")
    text = target.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            raise SystemExit(f"{path}: missing required text: {needle}")


require(
    "packaging/debian/update-package",
    "apt-get -qq",
    "install --only-upgrade -y teletool",
    "update-status.json",
)
require(
    "packaging/debian/teletool-update.service",
    "ExecStart=/usr/lib/teletool/bin/update-package",
)
require(
    "packaging/debian/teletool.sudoers",
    "/usr/bin/systemctl --no-block start teletool-update.service",
)
require(
    "scripts/build_deb.sh",
    "packaging/debian/teletool-update.service",
    "packaging/debian/update-package",
)
require(
    "app.py",
    '["systemctl", "--no-block", "start", "teletool-update.service"]',
    "_read_package_update_status",
)
require("static/system.html", 'btn.textContent = "Check for Update"')

for path in (ROOT / "app.py", ROOT / "static" / "system.html"):
    if "Managed by apt" in path.read_text(encoding="utf-8"):
        raise SystemExit(f"{path}: obsolete APT update lockout remains")

print("Package-managed Web updater is wired end to end.")
