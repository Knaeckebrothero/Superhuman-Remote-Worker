import pytest

from services.ssh_handles import HANDLE_PATTERN, is_valid_handle, mint_ssh_handle


def test_minted_handles_match_the_pattern():
    for _ in range(200):
        assert HANDLE_PATTERN.fullmatch(mint_ssh_handle())


def test_minted_handles_are_distinct():
    assert len({mint_ssh_handle() for _ in range(1000)}) == 1000


def test_excludes_ambiguous_characters():
    alphabet = set("".join(mint_ssh_handle()[2:] for _ in range(500)))
    assert not (alphabet & set("ilou"))


@pytest.mark.parametrize(
    "value",
    [
        "s-abcdefgh\nProxyCommand rm -rf /",   # newline injection into ssh_config
        "s-abcdefgh ProxyCommand x",           # space then a directive
        "s-ABCDEFGH",                          # uppercase
        "s-abcdefg",                           # too short
        "s-abcdefghi",                         # too long
        "abcdefgh",                            # missing prefix
        "s-abcdefgi",                          # excluded character
        "",
    ],
)
def test_rejects_anything_that_could_reach_ssh_config(value):
    """The handle is written verbatim into a user's ~/.ssh/config. Generated SSH
    config is an injection sink, so validate on output as well as at mint."""
    assert is_valid_handle(value) is False


def test_accepts_a_minted_handle():
    assert is_valid_handle(mint_ssh_handle()) is True
