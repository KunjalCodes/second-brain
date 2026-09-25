"""Phase 9 authentication: pure units (no database) -- password hashing, tokens,
credential/origin/client-IP helpers, rate-limit window arithmetic, and the
production-safety settings matrix (docs/PHASE9_PLAN.md section 15)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request

from app.auth.http import client_ip, extract_credential, origin_allowed
from app.auth.passwords import (
    ScryptParams,
    check_password_policy,
    hash_password,
    needs_rehash,
    normalize_email,
    verify_password,
)
from app.auth.tokens import is_well_formed, new_session_token, token_digest
from app.core.config import Settings
from app.core.errors import ValidationError
from app.services.rate_limit_service import retry_after, window_start

FAST = ScryptParams(n=2**10, r=8, p=1, maxmem=64 * 1024 * 1024)


def _request(
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    client: tuple[str, int] = ("127.0.0.1", 5000),
    host: str = "api.example.com",
    scheme: str = "https",
) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    raw.append((b"host", host.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": raw,
            "client": client,
            "scheme": scheme,
            "server": (host, 443),
            "query_string": b"",
        }
    )


def _settings(**kw: object) -> Settings:
    return Settings(**kw)  # type: ignore[arg-type]


def _prod(**kw: object) -> Settings:
    """A SAFE production configuration; each guard test breaks one property."""
    base: dict[str, object] = {
        "app_env": "production",
        "allow_user_id_header": False,
        "frontend_url": "https://brain.example.com",
        "auth_scrypt_n": 2**15,
        "auth_scrypt_r": 8,
        "auth_scrypt_p": 3,
    }
    base.update(kw)
    return _settings(**base)


class TestPasswords:
    def test_round_trip_and_wrong_password(self) -> None:
        stored = hash_password("correct horse battery", FAST)
        assert stored.startswith("scrypt$1024$8$1$")
        assert verify_password("correct horse battery", stored, maxmem=FAST.maxmem)
        assert not verify_password("correct horse batterx", stored, maxmem=FAST.maxmem)

    def test_hash_never_contains_the_password_and_is_salted(self) -> None:
        a, b = hash_password("same password!!", FAST), hash_password("same password!!", FAST)
        assert a != b
        assert "same password" not in a

    @pytest.mark.parametrize(
        "stored", ["", "plain", "bcrypt$x", "scrypt$1$2$3$@@@$###", "scrypt$a$b$c$d$e"]
    )
    def test_malformed_stored_hash_verifies_false_never_raises(self, stored: str) -> None:
        assert verify_password("anything goes here", stored, maxmem=FAST.maxmem) is False

    def test_needs_rehash_when_parameters_change(self) -> None:
        stored = hash_password("correct horse battery", FAST)
        assert needs_rehash(stored, FAST) is False
        assert needs_rehash(stored, ScryptParams(2**11, 8, 1, FAST.maxmem)) is True
        assert needs_rehash("garbage", FAST) is True

    def test_default_cost_needs_an_explicit_maxmem(self) -> None:
        """N=2^15/r=8 is 32 MiB: the OpenSSL default maxmem would reject it."""
        params = ScryptParams(2**15, 8, 1, 64 * 1024 * 1024)
        stored = hash_password("correct horse battery", params)
        assert verify_password("correct horse battery", stored, maxmem=params.maxmem)

    @pytest.mark.parametrize(
        ("password", "ok"),
        [("a" * 11, False), ("a" * 12, True), ("a" * 128, True), ("a" * 129, False)],
    )
    def test_length_policy_boundaries(self, password: str, ok: bool) -> None:
        if ok:
            check_password_policy(password, email_normalized="x@example.com")
        else:
            with pytest.raises(ValidationError):
                check_password_policy(password, email_normalized="x@example.com")

    def test_password_equal_to_email_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            check_password_policy("Longname@Example.com", email_normalized="longname@example.com")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("  Alice@Example.COM ", "alice@example.com"), ("a@b", "a@b")],
    )
    def test_email_normalisation(self, raw: str, expected: str) -> None:
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", "no-at-sign", "a@", "@b", "a b@c.d", "a@b@c", "x" * 250 + "@b.c"]
    )
    def test_invalid_emails(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            normalize_email(raw)


class TestTokens:
    def test_format_uniqueness_and_digest(self) -> None:
        tokens = {new_session_token() for _ in range(200)}
        assert len(tokens) == 200
        token = next(iter(tokens))
        assert is_well_formed(token) and token.startswith("sbs_") and len(token) == 47
        assert token_digest(token) == hashlib.sha256(token.encode()).hexdigest()
        assert len(token_digest(token)) == 64

    @pytest.mark.parametrize("bad", ["", "sbs_", "sbs_short", "x" * 47, "sbs_" + "!" * 43])
    def test_malformed_tokens_are_rejected_before_lookup(self, bad: str) -> None:
        assert not is_well_formed(bad)


class TestCredentialExtraction:
    def test_bearer_wins_over_cookie(self) -> None:
        s = _settings()
        c = extract_credential(
            _request(headers={"Authorization": "Bearer tok1", "Cookie": "sb_session=tok2"}), s
        )
        assert c is not None and (c.token, c.via) == ("tok1", "bearer")

    def test_cookie_when_no_authorization(self) -> None:
        c = extract_credential(_request(headers={"Cookie": "sb_session=tok2"}), _settings())
        assert c is not None and (c.token, c.via) == ("tok2", "cookie")

    @pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Token abc", "bearer  "])
    def test_malformed_authorization_is_an_attempt_with_an_empty_token(self, header: str) -> None:
        c = extract_credential(_request(headers={"Authorization": header}), _settings())
        assert c is not None and c.via == "bearer" and c.token in ("", header.split(" ", 1)[-1])
        if not header.lower().startswith("bearer"):
            assert c.token == ""

    def test_no_credential(self) -> None:
        assert extract_credential(_request(headers={"X-User-Id": "someone"}), _settings()) is None


class TestOriginCheck:
    def test_explicit_origin_allowed(self) -> None:
        s = _settings(frontend_url="https://brain.example.com")
        assert origin_allowed(_request(headers={"Origin": "https://brain.example.com"}), s)

    @pytest.mark.parametrize(
        "origin", [None, "https://evil.example.com", "https://brain.example.com.evil.io", "null"]
    )
    def test_missing_or_foreign_origin_refused(self, origin: str | None) -> None:
        s = _settings(frontend_url="https://brain.example.com")
        headers = {"Origin": origin} if origin else {}
        assert not origin_allowed(_request(headers=headers), s)

    def test_same_origin_allowed(self) -> None:
        req = _request(headers={"Origin": "https://api.example.com"}, host="api.example.com")
        assert origin_allowed(req, _settings())

    def test_named_vercel_project_preview_only(self) -> None:
        s = _settings(cors_allow_vercel_previews=True, cors_vercel_project="brain")
        assert origin_allowed(_request(headers={"Origin": "https://brain-git-x-me.vercel.app"}), s)
        assert not origin_allowed(_request(headers={"Origin": "https://evil.vercel.app"}), s)


class TestClientIp:
    def test_untrusted_peer_ignores_forwarded_for(self) -> None:
        req = _request(headers={"X-Forwarded-For": "9.9.9.9"}, client=("203.0.113.7", 1))
        assert client_ip(req, _settings()) == "203.0.113.7"
        assert client_ip(req, _settings(trusted_proxy_ips="10.0.0.0/8")) == "203.0.113.7"

    def test_trusted_proxy_uses_rightmost_untrusted_hop(self) -> None:
        s = _settings(trusted_proxy_ips="10.0.0.0/8,192.168.1.1")
        req = _request(
            headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.4, 10.1.2.3"},
            client=("192.168.1.1", 1),
        )
        # 6.6.6.6 is client-controlled; the proxy chain's own first untrusted
        # entry (right-most) is the real client.
        assert client_ip(req, s) == "198.51.100.4"

    def test_trusted_proxy_without_header_uses_peer(self) -> None:
        s = _settings(trusted_proxy_ips="127.0.0.1")
        assert client_ip(_request(client=("127.0.0.1", 1)), s) == "127.0.0.1"

    def test_garbage_forwarded_value_is_not_trusted(self) -> None:
        s = _settings(trusted_proxy_ips="127.0.0.1")
        req = _request(headers={"X-Forwarded-For": "not-an-ip"}, client=("127.0.0.1", 1))
        assert client_ip(req, s) == "127.0.0.1"


class TestRateLimitWindows:
    def test_window_start_is_aligned(self) -> None:
        now = datetime(2026, 1, 1, 12, 7, 42, tzinfo=UTC)
        assert window_start(now, 60) == datetime(2026, 1, 1, 12, 7, tzinfo=UTC)
        assert window_start(now, 900) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert window_start(now, 3600) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def test_retry_after_is_time_to_window_end(self) -> None:
        now = datetime(2026, 1, 1, 12, 7, 42, tzinfo=UTC)
        assert retry_after(now, 60) == 18
        assert retry_after(datetime(2026, 1, 1, 12, 7, 59, 500000, tzinfo=UTC), 60) == 1
        assert retry_after(now + timedelta(seconds=18), 60) == 60


class TestProductionSafety:
    def test_safe_production_config_is_accepted(self) -> None:
        s = _prod()
        assert s.effective_auth_mode == "session"
        assert s.cookie_secure is True
        assert s.registration_enabled is False

    @pytest.mark.parametrize("env", ["production", "staging", "prod", "", "Production"])
    def test_every_non_dev_environment_is_production_like(self, env: str) -> None:
        with pytest.raises(PydanticValidationError, match="ALLOW_USER_ID_HEADER"):
            _prod(app_env=env, allow_user_id_header=True)

    def test_dev_header_auth_mode_refused(self) -> None:
        with pytest.raises(PydanticValidationError, match="AUTH_MODE"):
            _prod(allow_user_id_header=None, auth_mode="dev_header")

    def test_insecure_cookie_refused(self) -> None:
        with pytest.raises(PydanticValidationError, match="AUTH_COOKIE_SECURE"):
            _prod(auth_cookie_secure=False)

    def test_missing_origin_allow_list_refused(self) -> None:
        with pytest.raises(PydanticValidationError, match="FRONTEND_URL or CORS_ORIGINS"):
            _prod(frontend_url="")

    @pytest.mark.parametrize(
        ("n", "r", "p"), [(2**14, 8, 5), (2**15, 8, 1), (2**16, 4, 4), (2**10, 8, 1)]
    )
    def test_weak_scrypt_refused(self, n: int, r: int, p: int) -> None:
        with pytest.raises(PydanticValidationError, match="OWASP"):
            _prod(auth_scrypt_n=n, auth_scrypt_r=r, auth_scrypt_p=p)

    @pytest.mark.parametrize(("n", "r", "p"), [(2**17, 8, 1), (2**16, 8, 2), (2**15, 8, 3)])
    def test_owasp_listed_scrypt_configurations_accepted(self, n: int, r: int, p: int) -> None:
        # Higher N needs a matching AUTH_SCRYPT_MAXMEM (validated separately).
        _prod(auth_scrypt_n=n, auth_scrypt_r=r, auth_scrypt_p=p, auth_scrypt_maxmem=2**29)

    def test_wildcard_vercel_previews_refused_everywhere(self) -> None:
        with pytest.raises(PydanticValidationError, match="CORS_VERCEL_PROJECT"):
            _settings(app_env="development", cors_allow_vercel_previews=True)

    def test_contradicting_identity_settings_refused(self) -> None:
        with pytest.raises(PydanticValidationError, match="contradict"):
            _settings(auth_mode="session", allow_user_id_header=True)

    def test_registration_can_be_explicitly_enabled_in_production(self) -> None:
        assert _prod(auth_registration_enabled=True).registration_enabled is True

    def test_development_defaults(self) -> None:
        s = _settings(app_env="development", allow_user_id_header=None)
        assert s.effective_auth_mode == "session"  # session is the default mode
        assert s.cookie_secure is False
        assert s.registration_enabled is True

    def test_scrypt_maxmem_must_cover_the_cost(self) -> None:
        with pytest.raises(PydanticValidationError, match="MAXMEM"):
            _settings(auth_scrypt_n=2**17, auth_scrypt_maxmem=32 * 1024 * 1024)


class TestSettingsParsing:
    def test_env_example_loads_as_is(self) -> None:
        """Copying .env.example verbatim must start (empty optional values
        mean "use the environment-dependent default")."""
        from pathlib import Path

        example = Path(__file__).resolve().parents[3] / ".env.example"
        s = Settings(_env_file=str(example))  # type: ignore[call-arg]
        assert s.cors_allow_vercel_previews is False

    @pytest.mark.parametrize(
        "field", ["auth_mode", "auth_registration_enabled", "auth_cookie_secure"]
    )
    def test_empty_optional_values_mean_unset(self, field: str) -> None:
        assert getattr(_settings(**{field: ""}), field) is None

    def test_malformed_trusted_proxy_list_refuses_to_start(self) -> None:
        with pytest.raises(PydanticValidationError):
            _settings(trusted_proxy_ips="10.0.0.0/8, not-an-ip")
