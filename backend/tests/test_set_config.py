import pytest

from scripts.set_config import apply_updates, main, parse_assignments


def test_parse_assignments_accepts_values_containing_equals():
    assert parse_assignments(["BASE_URL=https://x.ru/a=b"]) == {
        "BASE_URL": "https://x.ru/a=b"
    }


@pytest.mark.parametrize(
    "argument",
    ["NO_EQUALS_SIGN", "BAD KEY=value", "=value", "WITH-DASH=value"],
)
def test_parse_assignments_rejects_unusable_input(argument):
    with pytest.raises(ValueError):
        parse_assignments([argument])


def test_parse_assignments_rejects_an_empty_call():
    with pytest.raises(ValueError):
        parse_assignments([])


def test_apply_updates_keeps_comments_blank_lines_and_order():
    lines = ["# Network", "CADDY_DOMAIN=old", "", "BASE_URL=https://old"]

    updated, changed = apply_updates(lines, {"CADDY_DOMAIN": "new"})

    assert updated == ["# Network", "CADDY_DOMAIN=new", "", "BASE_URL=https://old"]
    assert changed == ["CADDY_DOMAIN"]


def test_apply_updates_appends_a_key_that_is_not_there_yet():
    updated, changed = apply_updates(["A=1"], {"B": "2"})

    assert updated == ["A=1", "B=2"]
    assert changed == ["B"]


def test_apply_updates_reports_nothing_when_the_value_already_matches():
    updated, changed = apply_updates(["A=1"], {"A": "1"})

    assert updated == ["A=1"]
    assert changed == []


def test_main_preserves_the_file_and_never_prints_the_secrets(tmp_path, capsys):
    env_file = tmp_path / "judge-helper.env"
    env_file.write_text("SECRET_KEY=do-not-print\nCADDY_DOMAIN=old\n", encoding="utf-8")
    env_file.chmod(0o640)
    inode = env_file.stat().st_ino

    assert main(["set_config.py", str(env_file), "CADDY_DOMAIN=new"]) == 0

    assert "CADDY_DOMAIN=new" in env_file.read_text(encoding="utf-8")
    # The app reads this path through a symlink, so the file must stay itself.
    assert env_file.stat().st_ino == inode
    assert env_file.stat().st_mode & 0o777 == 0o640

    backups = list(tmp_path.glob("judge-helper.env.*.bak"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o640

    assert "do-not-print" not in capsys.readouterr().out
