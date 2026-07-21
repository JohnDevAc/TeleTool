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
    "teletool-inferno=$target_version",
    "install --allow-downgrades",
    "apt-repo-dev",
    "update-status.json",
)
require(
    "packaging/apt/install.sh",
    "install --install-recommends -y teletool",
)
require(
    "packaging/debian/teletool-update@.service",
    "ExecStart=/usr/lib/teletool/bin/update-package %i",
)
require(
    "packaging/debian/teletool.sudoers",
    "/usr/bin/systemctl --no-block start teletool-update@main.service",
    "/usr/bin/systemctl --no-block start teletool-update@dev.service",
)
require(
    "scripts/build_deb.sh",
    "packaging/debian/teletool-update@.service",
    "packaging/debian/update-package",
    "TELETOOL_RELEASE_BRANCH",
)
require(
    "scripts/build_inferno_deb.sh",
    "Package: teletool-inferno",
    "alsa_pcm_inferno",
    "statime-linux",
    "INFERNO_REF",
    "STATIME_REF",
)
require(
    ".github/workflows/build-apt-package.yml",
    "teletool-arm64-dev-package",
    "TELETOOL_APT_SUITE: dev",
    "TELETOOL_RELEASE_BRANCH: dev",
    "scripts/build_inferno_deb.sh",
    "teletool-dev-apt",
    "teletool-stable-apt",
    "scripts/sign_apt_repo.sh",
    "git push --atomic origin HEAD:main HEAD:dev",
)
require(
    "scripts/sign_apt_repo.sh",
    "TELETOOL_APT_GPG_PRIVATE_KEY",
    "TELETOOL_APT_GPG_FINGERPRINT",
    "dpkg-deb -f",
    "verify_package teletool-inferno",
    "gpgv --keyring",
    "sha256sum",
)
require(
    "system_manager.py",
    'unit = f"teletool-update@{branch}.service"',
    "_read_package_update_status",
    "Updates require a package installation created by the published WGET installer.",
)
require(
    "static/system.html",
    'btn.textContent = "Check for Update"',
    'branchSelect.disabled = false',
)

for path in (ROOT / "app.py", ROOT / "system_manager.py", ROOT / "static" / "system.html"):
    if "Managed by apt" in path.read_text(encoding="utf-8"):
        raise SystemExit(f"{path}: obsolete APT update lockout remains")

python_source = "\n".join(
    path.read_text(encoding="utf-8")
    for path in (ROOT / "app.py", ROOT / "system_manager.py")
)
for obsolete in (
    "_run_program_update_worker",
    "_download_github_update_archive",
    "archive/refs/heads",
    "pi_full_setup.sh",
    "install_network_privileges.sh",
):
    if obsolete in python_source:
        raise SystemExit(f"Python source: unsupported source updater remains: {obsolete}")

for obsolete_path in (
    ".vscode",
    "deploy",
    "requirements.txt",
    "install_network_privileges.sh",
    "scripts/pi_full_setup.sh",
    "scripts/pi_make_golden_image.sh",
    "scripts/pi_setup.sh",
    "scripts/pi_sync.ps1",
):
    if (ROOT / obsolete_path).exists():
        raise SystemExit(f"Obsolete project artifact remains: {obsolete_path}")

require(
    "README.md",
    "## Install with WGET",
    "wget -qO- https://johndevac.github.io/TeleTool/apt-repo/install.sh | sudo sh",
)

print("Package-managed Web updater is wired end to end.")
