from dataclasses import dataclass


SUPPORTED_DISTROS = [
    {
        "id": "debian",
        "display": "Debian",
        "family": "debian-derivatives",
        "min_version": "12",
        "version_label": "Debian 12 (Bookworm) or Debian 13 (Trixie)",
        "package_manager": "apt",
    },
    {
        "id": "ubuntu",
        "display": "Ubuntu",
        "family": "debian-derivatives",
        "min_version": "24.04",
        "version_label": "Ubuntu 24.04 LTS (Noble Numbat)",
        "package_manager": "apt",
    },
]


@dataclass
class DistroInfo:
    distro: str
    version: str
    family: str
    package_manager: str
    supported: bool


class UnsupportedDistroError(Exception):
    pass


def _parse_os_release(content):
    result = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key] = value.strip('"').strip("'")
    return result


def _version_tuple(version_str):
    parts = []
    for segment in version_str.split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            break
    return tuple(parts)


def _raise_unsupported(distro_id=None, version_id=None):
    if distro_id is None:
        detected = "the host's distribution (could not parse /etc/os-release)"
    elif version_id:
        detected = f"{distro_id} {version_id}"
    else:
        detected = f"{distro_id} (unknown version)"

    supported_lines = "\n".join(f"  - {e['version_label']}" for e in SUPPORTED_DISTROS)

    raise UnsupportedDistroError(
        f"slop does not officially support\n"
        f"{detected}.\n"
        f"\n"
        f"Supported distros:\n"
        f"{supported_lines}\n"
        f"\n"
        f"See installer/SUPPORTED_DISTROS.md for the full support matrix.\n"
        f"To run slop on an unsupported distro, fork the project and\n"
        f"add the entry locally; this is not officially supported."
    )


def _detect_from_content(content):
    fields = _parse_os_release(content)
    distro_id = fields.get("ID")
    version_id = fields.get("VERSION_ID", "")

    if not distro_id:
        _raise_unsupported()

    for entry in SUPPORTED_DISTROS:
        if entry["id"] == distro_id:
            if _version_tuple(version_id) >= _version_tuple(entry["min_version"]):
                return DistroInfo(
                    distro=distro_id,
                    version=version_id,
                    family=entry["family"],
                    package_manager=entry["package_manager"],
                    supported=True,
                )
            _raise_unsupported(distro_id, version_id)

    _raise_unsupported(distro_id, version_id)


def detect_os(os_release_path="/etc/os-release"):
    try:
        with open(os_release_path) as f:
            content = f.read()
    except OSError:
        _raise_unsupported()
    return _detect_from_content(content)
