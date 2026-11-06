"""Managed proxy CA uploads remain valid, private in API metadata, and replaceable."""
import ssl
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mcp_manager import outbound
from mcp_manager.database import User

ENDPOINT = "/api/v1/settings/outbound-proxy"


def make_ca(name="proxy-test-ca"):
    key = ec.generate_private_key(ec.SECP256R1())
    identity = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    stamp = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(identity).issuer_name(identity)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(stamp - timedelta(days=1)).not_valid_after(stamp + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


async def test_ca_upload_validates_replaces_retains_and_removes(db_app):
    web, state = db_app.web, db_app.app.state
    initial = (await web.get(ENDPOINT)).json()
    assert "custom_ca_path" not in initial
    assert initial["custom_ca"] is None
    path = state.config.home_dir / "credentials" / "proxy-ca.pem"
    pem = make_ca()
    saved = await web.post(ENDPOINT + "/ca", json={"pem": pem})
    assert saved.status_code == 200, saved.text
    result = saved.json()
    assert result["custom_ca"]["filename"] == "proxy-ca.pem"
    assert result["custom_ca"]["available"] is True
    assert datetime.fromisoformat(result["custom_ca"]["uploaded_at"]).tzinfo is not None
    assert str(state.config.home_dir) not in saved.text
    assert pem not in saved.text
    assert path.read_text(encoding="utf-8") == pem
    ssl.create_default_context(cafile=str(path))
    assert (await outbound.load_config(state))["custom_ca_path"] == str(path)
    assert not list(path.parent.glob(".proxy-ca-*"))

    response = await web.patch(ENDPOINT, json={
        "enabled": True, "mode": "manual", "host": "proxy.example", "ssl_verify": "custom_ca",
    })
    assert response.status_code == 200, response.text
    before = response.json()
    rejected = await web.post(ENDPOINT + "/ca", json={"pem": "not a PEM CA"})
    assert rejected.status_code == 422 and "不是有效的 PEM CA 文件" in rejected.text
    assert path.read_text(encoding="utf-8") == pem
    assert (await web.get(ENDPOINT)).json() == before

    response = await web.patch(ENDPOINT, json={"ssl_verify": "default"})
    assert response.status_code == 200
    assert response.json()["custom_ca"] == result["custom_ca"] and path.exists()
    response = await web.patch(ENDPOINT, json={"ssl_verify": "custom_ca"})
    assert response.status_code == 200
    replacement = make_ca("replacement")
    response = await web.post(ENDPOINT + "/ca", json={"pem": replacement})
    assert response.status_code == 200
    assert response.json()["version"] > before["version"]
    assert path.read_text(encoding="utf-8") == replacement
    ssl.create_default_context(cafile=str(path))

    response = await web.delete(ENDPOINT + "/ca")
    assert response.status_code == 200, response.text
    assert response.json()["custom_ca"] is None
    assert response.json()["ssl_verify"] == "default" and not path.exists()
    assert response.json()["enabled"] is True
    assert (await outbound.load_config(state))["custom_ca_path"] is None
    assert (await web.patch(ENDPOINT, json={"ssl_verify": "custom_ca"})).status_code == 422
    assert (await web.patch(ENDPOINT, json={"custom_ca_path": "/server/private/ca.pem"})).status_code == 422


async def test_ca_upload_rechecks_admin_and_never_publishes_after_revocation(db_app, monkeypatch):
    from mcp_manager import outbound_ca

    original = outbound_ca.persist_ca
    async def revoke_after_validation(*args):
        async with db_app.db.locked() as session:
            (await session.get(User, db_app.actor["id"])).role = "user"
        return await original(*args)
    monkeypatch.setattr(outbound_ca, "persist_ca", revoke_after_validation)
    response = await db_app.web.post(ENDPOINT + "/ca", json={"pem": make_ca()})
    assert response.status_code == 403
    folder = db_app.app.state.config.home_dir / "credentials"
    assert not (folder / "proxy-ca.pem").exists()
    assert not list(folder.glob(".proxy-ca-*"))
    assert (await db_app.web.delete(ENDPOINT + "/ca")).status_code == 403


async def test_ca_write_rolls_back_file_when_database_commit_fails(db_app, monkeypatch):
    web, state = db_app.web, db_app.app.state
    original_pem = make_ca()
    assert (await web.post(ENDPOINT + "/ca", json={"pem": original_pem})).status_code == 200
    before = (await web.get(ENDPOINT)).json()
    original = state.db.locked
    @asynccontextmanager
    async def fail_commit():
        async with original() as session:
            yield session
            raise RuntimeError("fixture commit failure")
    monkeypatch.setattr(state.db, "locked", fail_commit)
    with pytest.raises(RuntimeError, match="fixture commit failure"):
        await web.post(ENDPOINT + "/ca", json={"pem": make_ca("rejected")})
    path = state.config.home_dir / "credentials" / "proxy-ca.pem"
    assert path.read_text(encoding="utf-8") == original_pem
    assert (await web.get(ENDPOINT)).json() == before
    assert not list(path.parent.glob(".proxy-ca-*"))
