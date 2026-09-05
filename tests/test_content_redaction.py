"""Presentation-boundary sanitizer — audit finding OC-05.

Two properties matter and they pull against each other. It must catch the
credential shapes a worker could copy into evidence or a routed question; and it
must NOT erase the identifiers an officer navigates by, because evidence he
cannot correlate is evidence he cannot use. The false-positive cases here are
as load-bearing as the true-positive ones.
"""

from shared.content_redaction import REDACTED, sanitize, sanitize_text


class TestSecretShapes:
    def test_bearer_tokens(self):
        r = sanitize("curl -H 'Authorization: Bearer abc123XYZ_-token'")
        assert "abc123XYZ_-token" not in r.text
        assert r.redacted

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1"
        assert jwt not in sanitize(f"token={jwt}").text

    def test_provider_key_prefixes(self):
        for secret in (
            "sk-abcdefghijklmnopqrstuvwxyz012345",
            "xoxb-1234567890-abcdefghij",
            "ghp_abcdefghijklmnopqrstuvwxyz0123",
            "AKIAIOSFODNN7EXAMPLE",
            "AIza" + "b" * 35,
        ):
            out = sanitize(f"the worker printed {secret} to stdout").text
            assert secret not in out, secret

    def test_key_value_forms(self):
        text = "password: hunter2\napi_key=deadbeefcafe\nclient_secret: 'shhh-9999'"
        out = sanitize(text).text
        assert "hunter2" not in out
        assert "deadbeefcafe" not in out
        assert "shhh-9999" not in out
        # The key NAME survives so the reader can see what was withheld.
        assert "password" in out and "api_key" in out

    def test_url_userinfo_keeps_the_host(self):
        # Which remote failed is diagnostic; the credential is not.
        out = sanitize(
            "fatal: could not read https://bob:s3cr3t@gitea.local/x.git"
        ).text
        assert "s3cr3t" not in out
        assert "gitea.local/x.git" in out

    def test_scp_style_remote(self):
        out = sanitize("git@host: deploy:pa55word@gitea.local:org/repo.git").text
        assert "pa55word" not in out

    def test_whole_pem_block_is_one_redaction(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA1234567890abcdef\n"
            "ZZZZmoreBase64Here0000000000000\n"
            "-----END RSA PRIVATE KEY-----"
        )
        r = sanitize(f"leaked:\n{pem}\ndone")
        assert "MIIEowIBAAKCAQEA" not in r.text
        assert "ZZZZmoreBase64Here" not in r.text
        assert r.count == 1  # not header-plus-surviving-body
        assert "done" in r.text

    def test_a_lone_pem_header_still_redacts(self):
        # Diagnostics often echo one line of a key rather than the whole block.
        assert (
            "BEGIN OPENSSH PRIVATE KEY"
            not in sanitize(
                "error near -----BEGIN OPENSSH PRIVATE KEY----- while parsing"
            ).text
        )


class TestKnownRuntimeSecrets:
    def test_exact_value(self):
        token = "workspace-token-abcdef123456"
        out = sanitize(f"cloned with {token}", secrets=[token]).text
        assert token not in out

    def test_url_encoded_form(self):
        secret = "p@ss/word+value"
        text = "https://x/?t=p%40ss%2Fword%2Bvalue"
        assert "p%40ss" not in sanitize(text, secrets=[secret]).text

    def test_a_short_secret_is_ignored_rather_than_shredding_the_text(self):
        # An 8-char floor: erasing every occurrence of a 3-char "secret" would
        # destroy the surrounding prose and tell the reader nothing.
        out = sanitize("the cat sat on the mat", secrets=["cat"]).text
        assert out == "the cat sat on the mat"

    def test_longest_variant_wins(self):
        # Redacting a prefix first would strand the tail as an orphan fragment.
        out = sanitize(
            "value=abcdef123456789", secrets=["abcdef12", "abcdef123456789"]
        ).text
        assert "abcdef" not in out


class TestFalsePositives:
    """What must SURVIVE. An officer correlates evidence by these."""

    def test_ids_hashes_and_paths_survive(self):
        text = (
            "job 1ad5d2a0-8a67-418c-947d-b2112292f230 commit 9e4c8d63a1 "
            "sha256:3112549f0613c96a8012cea6a1f06f4b6249ec43c7b1c4a70161ba27b18af37e "
            "wrote projects/resavio/report.md (4051 bytes)"
        )
        r = sanitize(text)
        assert r.text == text
        assert not r.redacted

    def test_ordinary_prose_is_untouched(self):
        text = "The deploy failed because the health check timed out after 30s."
        assert sanitize(text).text == text

    def test_the_word_token_alone_is_not_a_secret(self):
        # Only key=value shapes trigger; a sentence mentioning tokens does not.
        text = "The token budget was exceeded twice this week."
        assert sanitize(text).text == text


class TestReporting:
    def test_count_lets_a_surface_say_something_was_withheld(self):
        r = sanitize("a=1 password: one api_key=two")
        assert r.count == 2 and r.redacted

    def test_clean_text_reports_zero(self):
        r = sanitize("nothing to see")
        assert r.count == 0 and not r.redacted

    def test_empty_and_none_are_safe(self):
        assert sanitize(None).text == "" and not sanitize(None).redacted
        assert sanitize("").count == 0
        assert sanitize_text(None) == ""

    def test_sanitize_text_is_the_no_report_convenience(self):
        assert REDACTED in sanitize_text("password: hunter2")
