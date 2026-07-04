import pytest

from installer.detect import (
    _detect_from_content,
    detect_os,
    UnsupportedDistroError,
)

DEBIAN_12 = "ID=debian\nVERSION_ID=12\n"
DEBIAN_13 = "ID=debian\nVERSION_ID=13\n"
UBUNTU_22_04 = "ID=ubuntu\nVERSION_ID=22.04\n"
UBUNTU_24_04 = "ID=ubuntu\nVERSION_ID=24.04\n"
UBUNTU_20_04 = "ID=ubuntu\nVERSION_ID=20.04\n"
FEDORA_40 = "ID=fedora\nVERSION_ID=40\n"
DEBIAN_12_QUOTED = 'ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'


class TestPassCases:
    def test_debian_12(self):
        info = _detect_from_content(DEBIAN_12)
        assert info.distro == "debian"
        assert info.version == "12"
        assert info.family == "debian-derivatives"
        assert info.package_manager == "apt"
        assert info.supported is True

    def test_debian_13(self):
        info = _detect_from_content(DEBIAN_13)
        assert info.distro == "debian"
        assert info.version == "13"
        assert info.supported is True

    def test_ubuntu_24_04(self):
        info = _detect_from_content(UBUNTU_24_04)
        assert info.distro == "ubuntu"
        assert info.version == "24.04"
        assert info.family == "debian-derivatives"
        assert info.package_manager == "apt"
        assert info.supported is True

    def test_quoted_version_id_parsed_correctly(self):
        info = _detect_from_content(DEBIAN_12_QUOTED)
        assert info.distro == "debian"
        assert info.version == "12"
        assert info.supported is True


class TestFailBelowMinimum:
    def test_ubuntu_20_04_rejected(self):
        with pytest.raises(UnsupportedDistroError) as exc_info:
            _detect_from_content(UBUNTU_20_04)
        assert "ubuntu 20.04" in str(exc_info.value)

    def test_ubuntu_22_04_rejected(self):
        with pytest.raises(UnsupportedDistroError) as exc_info:
            _detect_from_content(UBUNTU_22_04)
        assert "ubuntu 22.04" in str(exc_info.value)

    def test_error_message_names_supported_distros(self):
        with pytest.raises(UnsupportedDistroError) as exc_info:
            _detect_from_content(UBUNTU_20_04)
        msg = str(exc_info.value)
        assert "Debian 12" in msg
        assert "Ubuntu 24.04" in msg

    def test_error_message_references_doc(self):
        with pytest.raises(UnsupportedDistroError) as exc_info:
            _detect_from_content(UBUNTU_20_04)
        assert "installer/SUPPORTED_DISTROS.md" in str(exc_info.value)


class TestUnsupportedFamily:
    def test_fedora_40_rejected(self):
        with pytest.raises(UnsupportedDistroError) as exc_info:
            _detect_from_content(FEDORA_40)
        assert "fedora 40" in str(exc_info.value)

    def test_fedora_40_error_lists_supported(self):
        with pytest.raises(UnsupportedDistroError) as exc_info:
            _detect_from_content(FEDORA_40)
        msg = str(exc_info.value)
        assert "Debian 12" in msg
        assert "Ubuntu 24.04" in msg


class TestCannotDetect:
    def test_missing_os_release_file(self, tmp_path):
        nonexistent = str(tmp_path / "no_such_os_release")
        with pytest.raises(UnsupportedDistroError) as exc_info:
            detect_os(os_release_path=nonexistent)
        assert "could not parse /etc/os-release" in str(exc_info.value)

    def test_missing_id_field(self):
        content = "PRETTY_NAME=SomeLinux\nVERSION_ID=42\n"
        with pytest.raises(UnsupportedDistroError) as exc_info:
            _detect_from_content(content)
        assert "could not parse /etc/os-release" in str(exc_info.value)
