"""Reject local deployment identifiers in tracked files before publication."""

import argparse
import ipaddress
import re
import subprocess
from pathlib import Path

IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6 = re.compile(r"(?i)(?<![\w:])(?:f[cd][\da-f]{0,2}|fe[89ab][\da-f]):[\da-f:]+")
GPU = re.compile(r"\bGPU-[\da-fA-F]{8}(?:-[\da-fA-F]{4}){3}-[\da-fA-F]{12}\b")
HOST_PATH = re.compile(r"\/(?:opt|home|Users)\/[\w.-]+|\/root\/(?!\.cache/pip(?:/|\b))[\w.-]+")
PRIVATE_DIRS = {".tools", ".local", "models", "data", "test-results", "private"}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".crt", ".db", ".sqlite", ".safetensors"}


def scan_text(name, source):
    findings = []
    for number, line in enumerate(source.splitlines(), 1):
        for match in IPV4.finditer(line):
            if (name.endswith((".lock", ".in"))
                    and re.fullmatch(r"\s*[\w.\[\]-]+==", line[:match.start()])):
                continue  # Four-part dependency versions are not network addresses.
            try:
                octets = ipaddress.IPv4Address(match.group()).packed
            except ipaddress.AddressValueError:
                continue
            if (octets[0] == 10 or octets[0] == 172 and 16 <= octets[1] <= 31
                    or octets[:2] == bytes([192, 168])):
                findings.append((name, number, "private IPv4 address"))
        for match in IPV6.finditer(line):
            try:
                address = ipaddress.IPv6Address(match.group())
            except ipaddress.AddressValueError:
                continue
            if address.is_private or address.is_link_local:
                findings.append((name, number, "private IPv6 address"))
        if GPU.search(line):
            findings.append((name, number, "GPU device UUID"))
        if HOST_PATH.search(line):
            findings.append((name, number, "host-specific absolute path"))
    return findings


def main(staged=False):
    command = ["git", "ls-files", "-z"]
    if staged:
        command.insert(2, "--stage")
    entries = subprocess.check_output(command).split(b"\0")
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    findings, count = [], 0
    for entry in filter(None, entries):
        if staged:
            metadata, path = entry.split(b"\t", 1)
            _, oid, stage = metadata.split()
            if stage != b"0":
                raise ValueError("Resolve index conflicts before publication")
            content = subprocess.check_output(["git", "cat-file", "blob", oid.decode()])
        else:
            path = entry
            content = (root / path.decode()).read_bytes()
        name = path.decode()
        parts = Path(name).parts
        if (PRIVATE_DIRS.intersection(parts) or any(part.startswith(".venv") for part in parts)
                or Path(name).suffix in PRIVATE_SUFFIXES
                or Path(name).name.startswith(".env") and name != ".env.example"):
            findings.append((name, 0, "private file or runtime artifact"))
        count += 1
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            continue  # Screenshots additionally require visual review.
        findings.extend(scan_text(name, content.decode("utf-8")))
    for name, line, reason in findings:
        print(f"{name}:{line}: {reason}")  # Do not print identifiers into public CI logs.
    if findings:
        raise SystemExit(1)
    print(f"PUBLIC_CONTENT_PASS ({count} files; no deployment identifiers)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Scan the index that will be committed")
    main(parser.parse_args().staged)
