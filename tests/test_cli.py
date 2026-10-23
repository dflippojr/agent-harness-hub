"""The command line: the installer's launch contract and the sign-in link."""

from __future__ import annotations

import json

import pytest

from harness_hub import __main__ as cli
from harness_hub.config import Settings, normalize_daemon_url, normalize_public_url


def test_installer_arguments_parse(tmp_path):
    args = cli.serve_parser().parse_args(["--daemon-url", "http://127.0.0.1:8100", "--claim-file",
                                          str(tmp_path / "claim.json"), "--state-dir", str(tmp_path)])
    assert args.daemon_url == "http://127.0.0.1:8100" and args.port == 8120 and args.host == "127.0.0.1"


def test_signin_link_uses_the_running_hubs_url(tmp_path, capsys):
    (tmp_path / "server.json").write_text(json.dumps({"url": "https://hub.example.ts.net"}), encoding="utf-8")
    assert cli.main(["signin-link", "--state-dir", str(tmp_path)]) == 0
    link = capsys.readouterr().out.strip()
    assert link.startswith("https://hub.example.ts.net/#signin=")
    assert len(link.split("#signin=", 1)[1]) >= 40


def test_signin_link_default_url(tmp_path, capsys):
    cli.main(["signin-link", "--state-dir", str(tmp_path)])
    assert capsys.readouterr().out.startswith("http://127.0.0.1:8120/#signin=")


@pytest.mark.parametrize("url", ["ftp://x", "http://user:pw@host", "http://host/path", "http://host?q=1", "nohost"])
def test_bad_daemon_urls(url):
    with pytest.raises(ValueError):
        normalize_daemon_url(url)


def test_public_url_must_be_https_off_loopback():
    assert normalize_public_url("https://hub.example.ts.net/") == "https://hub.example.ts.net"
    assert normalize_public_url("http://127.0.0.1:8120") == "http://127.0.0.1:8120"
    with pytest.raises(ValueError):
        normalize_public_url("http://hub.example.ts.net")


def test_allowed_hosts():
    settings = Settings(port=8120, public_url="https://hub.example.ts.net", extra_hosts=("lan-box:8120",))
    hosts = settings.allowed_hosts()
    assert {"127.0.0.1:8120", "localhost:8120", "[::1]:8120", "hub.example.ts.net", "lan-box:8120"} <= hosts
    assert settings.secure_cookies


def test_bad_arguments_exit(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--daemon-url", "ftp://nope", "--state-dir", str(tmp_path)])


def test_signin_link_refuses_remote_http(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["signin-link", "--state-dir", str(tmp_path), "--url", "http://192.168.1.20:8120"])
    assert not (tmp_path / "signin-codes.json").exists()


def _running_hub(state_dir, url):
    (state_dir / "server.json").write_text(json.dumps({"url": url}), encoding="utf-8")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8120", "http://localhost:9000", "http://[::1]:8120",
                                 "http://127.0.0.1:8120/"])
def test_signin_link_refuses_loopback_http_on_an_https_hub(tmp_path, capsys, url):
    # The Hub refuses every write from an http loopback origin once it has an https public URL (#133).
    _running_hub(tmp_path, "https://hub.example.ts.net")
    assert cli.main(["signin-link", "--state-dir", str(tmp_path), "--url", url]) != 0
    out, err = capsys.readouterr()
    assert "#signin=" not in out and out.strip() == ""
    assert "https://hub.example.ts.net" in err and "cannot work" in err
    assert "without --public-url" in err
    assert not (tmp_path / "signin-codes.json").exists()


@pytest.mark.parametrize(("server", "url", "expected"), [
    ("https://hub.example.ts.net", "", "https://hub.example.ts.net/#signin="),
    ("https://hub.example.ts.net", "https://hub.example.ts.net", "https://hub.example.ts.net/#signin="),
    ("https://hub.example.ts.net", "https://other.example.ts.net", "https://other.example.ts.net/#signin="),
    ("http://127.0.0.1:8120", "http://127.0.0.1:8120", "http://127.0.0.1:8120/#signin="),
    ("http://127.0.0.1:8120", "http://localhost:9000", "http://localhost:9000/#signin="),
    ("", "http://[::1]:8120", "http://[::1]:8120/#signin="),
])
def test_signin_link_still_prints_links_that_can_work(tmp_path, capsys, server, url, expected):
    if server:
        _running_hub(tmp_path, server)
    assert cli.main(["signin-link", "--state-dir", str(tmp_path)] + (["--url", url] if url else [])) == 0
    assert capsys.readouterr().out.startswith(expected)
    assert (tmp_path / "signin-codes.json").exists()


def test_signin_link_refuses_remote_http_on_an_https_hub(tmp_path):
    _running_hub(tmp_path, "https://hub.example.ts.net")
    with pytest.raises(SystemExit):
        cli.main(["signin-link", "--state-dir", str(tmp_path), "--url", "http://192.168.1.20:8120"])
    assert not (tmp_path / "signin-codes.json").exists()


def _home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return home, cwd


@pytest.mark.parametrize("argv", [["--state-dir", "~/hub"], ["--state-dir=~/hub"]])
def test_signin_link_expands_a_tilde_state_dir(tmp_path, monkeypatch, capsys, argv):
    # `--state-dir=~/...` reaches the program unexpanded in bash and sh; serve expands it, so signin-link must (#172).
    home, cwd = _home(monkeypatch, tmp_path)
    assert cli.main(["signin-link", *argv]) == 0
    assert capsys.readouterr().out.startswith("http://127.0.0.1:8120/#signin=")
    assert (home / "hub" / "signin-codes.json").exists()
    assert list(cwd.iterdir()) == []


def test_signin_link_reads_the_running_hub_from_a_tilde_state_dir(tmp_path, monkeypatch, capsys):
    home, cwd = _home(monkeypatch, tmp_path)
    (home / "hub").mkdir()
    _running_hub(home / "hub", "https://hub.example.ts.net")
    assert cli.main(["signin-link", "--state-dir", "~/hub", "--url", "http://127.0.0.1:8120"]) != 0
    assert "#signin=" not in capsys.readouterr().out
    assert not (home / "hub" / "signin-codes.json").exists()
    assert list(cwd.iterdir()) == []


@pytest.mark.parametrize("raw", ["~/hub", "~/hub/../hub", "hub", "./sub/../hub"])
def test_signin_link_writes_where_serve_reads(tmp_path, monkeypatch, capsys, raw):
    _home(monkeypatch, tmp_path)
    served = []
    monkeypatch.setattr("harness_hub.server.create_app", lambda settings: settings)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: served.append(app.state_dir))
    assert cli.main(["--state-dir", raw]) == 0
    assert cli.main(["signin-link", "--state-dir", raw]) == 0
    capsys.readouterr()
    assert (served[0] / "signin-codes.json").exists()


def test_signin_link_default_state_dir(tmp_path, monkeypatch, capsys):
    _, cwd = _home(monkeypatch, tmp_path)
    default = tmp_path / "default"
    monkeypatch.setattr(cli, "DEFAULT_STATE_DIR", default)
    assert cli.main(["signin-link"]) == 0
    assert capsys.readouterr().out.startswith("http://127.0.0.1:8120/#signin=")
    assert (default / "signin-codes.json").exists()
    assert list(cwd.iterdir()) == []
