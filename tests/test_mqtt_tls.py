"""TLS context construction.

A home-lab Mosquitto usually presents a private or self-signed certificate, so
`MQTT_TLS=true` against the system CA store fails. These cover the CA-file path
(verification stays on) and the deliberate escape hatch.
"""

from __future__ import annotations

import ssl

import pytest

from src.config import ConfigError
from src.mqtt_client import WatchdogMqtt

# A real self-signed CA (openssl, RSA-2048, expires 2046) so
# load_verify_locations genuinely parses it rather than erroring.
CA_PEM = """-----BEGIN CERTIFICATE-----
MIIDDzCCAfegAwIBAgIUJ6XPgZra8VrwtLGz/pc1DP7vHlQwDQYJKoZIhvcNAQEL
BQAwFzEVMBMGA1UEAwwMVGVzdCBSb290IENBMB4XDTI2MDgwODEzMTUzMVoXDTQ2
MDgwMzEzMTUzMVowFzEVMBMGA1UEAwwMVGVzdCBSb290IENBMIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEAj5qvc0mHeUOqQuTrwhJ1SCOLr6MPqsZbgzTx
MAfAaM8r/XQ3wbkIE/OQ9zHZvf6pmmMwtgeNSKc6m/kgx0ms2bjOzMAGE4H3+sk1
fcjmPk3Y8EZczBq/q4ugOgFYi2/6cms86FTIU5mg5iVLEGiPn9j6FzraUC5knoJs
fk9G1uCM63V4+ktjlqfcMvUORaGkSpGm87hcKJB4Uzina/J3IAB+W4xkJHf/tXOF
oZOvaEMS3SS/Vr8L+CPw1NrvlC6LRFj3jt6A75bMUBG/HnXATi1Ng4SPWvyi9IQA
ogBL89voRoyPJhybWdSls2Y5BPt7FQItK25MGi2XDbGzR8C5YQIDAQABo1MwUTAd
BgNVHQ4EFgQUcQWuPU16apq1fdoErnYVueWpOdAwHwYDVR0jBBgwFoAUcQWuPU16
apq1fdoErnYVueWpOdAwDwYDVR0TAQH/BAUwAwEB/zANBgkqhkiG9w0BAQsFAAOC
AQEAD+pwNZVJB92QHEc+5S+aosxcBQLf9kD9HelBpAAlbqBx6Bhzv3a1LFA/xOpw
Di6r30039TM8yJWiVkWalTAM6JFj6/mNU/Qvc+k5H+ZJP/14fsOCCqhFAdAnuJwT
M7f1dqAh3b9wGSFrQLlGOxFfTBRjSFIbGz5HaVmmQ41c7FZBoGbXfLL+fdu/u8uB
cEQCAkI2RrAPOOCdEf/c40OUzBWbRabzwoL79+VFb76NSJjDyUJWYETbQPuCeQ87
oNHdvNlBFR5N3PWYDVdxXhyRNEoetkRKG0NGAzbnD4Nrs+k/L/ocvb2AOcWzC5Kg
AJHEJtvYJzodPEUmwRfldBgysg==
-----END CERTIFICATE-----
"""


def context_for(make_config, **overrides) -> ssl.SSLContext:
    return WatchdogMqtt._build_tls_context(make_config(**overrides))


def test_default_tls_verifies_against_system_cas(make_config):
    context = context_for(make_config, MQTT_TLS="true")
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_a_custom_ca_keeps_verification_on(make_config, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text(CA_PEM)

    context = context_for(make_config, MQTT_TLS="true", MQTT_TLS_CA_CERT=str(ca))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    # The CA really was loaded, not silently ignored.
    assert len(context.get_ca_certs()) >= 1


def test_insecure_mode_disables_verification(make_config):
    context = context_for(make_config, MQTT_TLS="true", MQTT_TLS_INSECURE="true")
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_insecure_mode_does_not_raise_on_ordering(make_config):
    """check_hostname must be cleared before verify_mode or Python raises."""
    context_for(make_config, MQTT_TLS="true", MQTT_TLS_INSECURE="true")


def test_insecure_wins_over_a_supplied_ca(make_config, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text(CA_PEM)

    context = context_for(
        make_config, MQTT_TLS="true", MQTT_TLS_CA_CERT=str(ca), MQTT_TLS_INSECURE="true"
    )
    assert context.verify_mode == ssl.CERT_NONE


def test_client_construction_with_tls_does_not_raise(make_config, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text(CA_PEM)
    WatchdogMqtt(make_config(MQTT_TLS="true", MQTT_TLS_CA_CERT=str(ca)))


def test_a_malformed_ca_gives_a_readable_error(make_config, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("this is not a certificate")

    with pytest.raises(ConfigError, match="not a readable PEM certificate"):
        context_for(make_config, MQTT_TLS="true", MQTT_TLS_CA_CERT=str(ca))
