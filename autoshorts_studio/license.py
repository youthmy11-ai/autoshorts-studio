"""Offline deterministic Pro license key verification and tier configuration."""

from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

PRICING_URL = "https://youthmy11-ai.github.io/#pricing"
PRODUCT_CODE = "AS"
ACCEPTED_PRODUCTS: tuple[str, ...] = ("AS", "BUNDLE")

COMMUNITY_MAX_BATCH_CLIPS = 5
PRO_MAX_BATCH_CLIPS = 500

_KEY_PATTERN = re.compile(r"^FS-PRO-(AS|WL|BUNDLE)-([A-Z0-9]{8})-([0-9A-F]{4})$")


@dataclass(frozen=True)
class LicenseStatus:
    """Resolved licensing tier and capability flags."""

    is_pro: bool
    tier: str
    product_code: str | None
    masked_key: str | None
    reason: str
    max_batch_clips: int
    allow_custom_watermark: bool
    allow_multi_preset: bool

    @property
    def upgrade_url(self) -> str:
        return PRICING_URL


def _community_status(reason: str) -> LicenseStatus:
    return LicenseStatus(
        is_pro=False,
        tier="community",
        product_code=None,
        masked_key=None,
        reason=reason,
        max_batch_clips=COMMUNITY_MAX_BATCH_CLIPS,
        allow_custom_watermark=False,
        allow_multi_preset=False,
    )


def _pro_status(product_code: str, masked_key: str, reason: str) -> LicenseStatus:
    return LicenseStatus(
        is_pro=True,
        tier="pro",
        product_code=product_code,
        masked_key=masked_key,
        reason=reason,
        max_batch_clips=PRO_MAX_BATCH_CLIPS,
        allow_custom_watermark=True,
        allow_multi_preset=True,
    )


def compute_license_checksum(product_code: str, payload: str) -> str:
    """Compute the deterministic 4-hex-digit FNV-1a folded checksum for a license payload."""
    normalized_product = product_code.strip().upper()
    normalized_payload = payload.strip().upper()
    if normalized_product not in ("AS", "WL", "BUNDLE"):
        raise ValueError(f"Unsupported product code: {product_code!r}")
    if not re.fullmatch(r"[A-Z0-9]{8}", normalized_payload):
        raise ValueError("License payload must be exactly 8 alphanumeric characters.")

    token = f"FS-PRO-{normalized_product}-{normalized_payload}".encode("ascii")
    h = 0x811C9DC5
    for byte in token:
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    folded = ((h >> 16) ^ (h & 0xFFFF)) & 0xFFFF
    return f"{folded:04X}"


def format_license_key(product_code: str, payload: str) -> str:
    """Build a canonical license key string from product code and 8-character payload."""
    normalized_product = product_code.strip().upper()
    normalized_payload = payload.strip().upper()
    checksum = compute_license_checksum(normalized_product, normalized_payload)
    return f"FS-PRO-{normalized_product}-{normalized_payload}-{checksum}"


def mask_license_key(key: str) -> str:
    """Return a masked display string preserving prefix and trailing checksum."""
    normalized = key.strip().upper()
    match = _KEY_PATTERN.match(normalized)
    if not match:
        return "****"
    product_code, payload, checksum = match.groups()
    return f"FS-PRO-{product_code}-{payload[:2]}******-{checksum}"


def validate_license_key(
    key: str | None,
    accepted_products: tuple[str, ...] = ACCEPTED_PRODUCTS,
) -> LicenseStatus:
    """Validate an offline Pro license key without any external network call."""
    if not key or not isinstance(key, str) or not key.strip():
        return _community_status("Community Edition active (no Pro key configured).")

    normalized = key.strip().upper()
    match = _KEY_PATTERN.match(normalized)
    if not match:
        return _community_status(
            "Invalid license key format. Expected FS-PRO-AS-XXXXXXXX-XXXX or FS-PRO-BUNDLE-XXXXXXXX-XXXX."
        )

    product_code, payload, provided_checksum = match.groups()
    if payload == "00000000":
        return _community_status("Rejected placeholder license payload.")

    expected_checksum = compute_license_checksum(product_code, payload)
    if not hmac.compare_digest(provided_checksum, expected_checksum):
        return _community_status("License checksum mismatch.")

    if product_code not in accepted_products:
        return _community_status(
            f"License key is valid for product '{product_code}', not AutoShorts Studio."
        )

    tier_label = "Suite Bundle Pro" if product_code == "BUNDLE" else "AutoShorts Studio Pro"
    return _pro_status(
        product_code=product_code,
        masked_key=mask_license_key(normalized),
        reason=f"{tier_label} unlocked.",
    )


def default_config_dir() -> Path:
    """Return the per-user configuration directory without hardcoding local paths."""
    env_dir = os.environ.get("AUTOSHORTS_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".autoshorts_studio"


def _license_file(config_dir: Path | None = None) -> Path:
    base = Path(config_dir) if config_dir is not None else default_config_dir()
    return base / "license.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".lic_",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        json.dump(data, tmp, indent=2)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def save_license_key(key: str, config_dir: Path | None = None) -> LicenseStatus:
    """Validate and persist a Pro license key to the local configuration file."""
    status = validate_license_key(key)
    if not status.is_pro:
        raise ValueError(status.reason)
    target = _license_file(config_dir)
    _atomic_write_json(
        target,
        {
            "license_key": key.strip().upper(),
            "product_code": status.product_code,
            "tier": status.tier,
        },
    )
    return status


def clear_license_key(config_dir: Path | None = None) -> None:
    """Remove any locally stored Pro license key."""
    target = _license_file(config_dir)
    if target.exists():
        target.unlink()


def load_license_status(
    config_dir: Path | None = None,
    explicit_key: str | None = None,
) -> LicenseStatus:
    """Resolve effective license status from explicit key, environment, or local config."""
    if explicit_key is not None and explicit_key.strip():
        return validate_license_key(explicit_key)

    env_key = os.environ.get("AUTOSHORTS_LICENSE_KEY")
    if env_key and env_key.strip():
        return validate_license_key(env_key)

    target = _license_file(config_dir)
    if not target.exists():
        return _community_status("Community Edition active (no Pro key configured).")

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _community_status(f"Could not read saved license file: {exc}")

    saved_key = data.get("license_key") if isinstance(data, dict) else None
    return validate_license_key(saved_key)
