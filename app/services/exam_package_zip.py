"""Build a signed *exam package* ZIP for a single station.

This is a small artifact (not a runtime bundle): only the signed manifest,
scope-only seed, machine credential payload, and file hashes. The Chief IT
imports it into an already-installed Station via the local admin UI.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile


def _canonical(data) -> bytes:
    return json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_exam_package_zip(package_bundle: dict) -> bytes:
    """Return the exam-package ZIP for the given assembled bundle.

    ``package_bundle`` is what :func:`station_package.generate_station_package`
    persists on ``StationPackage.manifest`` — it contains ``manifest``,
    ``seed``, ``signature``, and (transiently) ``machine_credential``.
    """
    manifest = package_bundle.get("manifest") or {}
    seed = package_bundle.get("seed") or {}
    signature = package_bundle.get("signature") or ""
    machine_credential = package_bundle.get("machine_credential") or {}

    manifest_bytes = _canonical(manifest)
    seed_bytes = _canonical(seed)
    credential_bytes = _canonical(machine_credential)
    signature_bytes = signature.encode("ascii")

    sha_lines = [
        f"{_sha256_hex(manifest_bytes)}  manifest.json",
        f"{_sha256_hex(seed_bytes)}  seed.json",
        f"{_sha256_hex(credential_bytes)}  machine-credential.json",
        f"{_sha256_hex(signature_bytes)}  signature",
    ]
    sha_bytes = ("\n".join(sha_lines) + "\n").encode("ascii")

    readme = (
        "LAZEIMS Exam Package\n"
        "====================\n\n"
        f"Station : {manifest.get('station_code', '?')}\n"
        f"Exam    : {manifest.get('exam_name') or manifest.get('exam_code') or manifest.get('exam_id')}\n"
        f"Version : {manifest.get('package_version')}\n\n"
        "Import this file from the local LAZEIMS Station admin console\n"
        "(http://<station-ip>:8080). The Station uses one persistent installation\n"
        "for every exam and every package — do NOT extract this ZIP by hand.\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        zf.writestr("seed.json", seed_bytes)
        zf.writestr("machine-credential.json", credential_bytes)
        zf.writestr("signature", signature_bytes)
        zf.writestr("SHA256SUMS", sha_bytes)
        zf.writestr("README.txt", readme)
    return buf.getvalue()
