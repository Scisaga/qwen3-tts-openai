import pytest

from scripts.check_public_content import scan_text


@pytest.mark.parametrize("octets", [(10, 23, 4, 5), (172, 20, 3, 4), (192, 168, 2, 3)])
def test_private_deployment_addresses_are_reported_without_values(octets):
    address = ".".join(map(str, octets))
    findings = scan_text("README.md", f"ASR_BASE_URL=http://{address}:8001")
    assert findings == [("README.md", 1, "private IPv4 address")]
    assert address not in repr(findings)


@pytest.mark.parametrize("address", ["localhost", "127.0.0.1", "0.0.0.0", "asr.example.test"])
def test_generic_examples_and_bind_addresses_are_allowed(address):
    assert not scan_text("README.md", f"ASR_BASE_URL=http://{address}:8001")


def test_host_paths_and_device_identifiers_are_reported():
    host_path = "/" + "opt" + "/" + "private-project"
    device = "GPU-" + "12345678" + "-1234" * 3 + "-123456789abc"
    assert scan_text("docs/testing.md", host_path) == [("docs/testing.md", 1, "host-specific absolute path")]
    assert scan_text("README.md", device) == [("README.md", 1, "GPU device UUID")]
    assert not scan_text("Dockerfile", "pip cache: /root/.cache/pip")


def test_private_ipv6_addresses_are_reported():
    address = "fd" + "00::1"
    assert scan_text("README.md", f"https://[{address}]") == [("README.md", 1, "private IPv6 address")]


def test_dependency_versions_do_not_hide_private_index_urls():
    version = ".".join(map(str, (10, 3, 7, 77)))
    assert not scan_text("requirements-gpu.lock", f"nvidia-curand-cu12=={version}")
    assert scan_text("requirements.lock", f"--index-url http://{version}/simple")
