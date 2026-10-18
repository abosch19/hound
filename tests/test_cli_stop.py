"""La orden "hound stop" tal como la lanza el servicio de Windows."""

from __future__ import annotations

import pytest

from hound.cli import main


def test_stop_ignora_los_argumentos_que_anade_winsw(tmp_path, capsys):
    config = str(tmp_path / "config.toml")
    (tmp_path / "share").mkdir()
    main(["--config", config, "init", "--share", str(tmp_path / "share"), "--data-dir", str(tmp_path / "data"),
          "--port", "18799", "--permissions", "none"])
    # WinSW 2 ejecuta <stoparguments> seguido de <arguments>.
    assert main(["--config", config, "stop", "-m", "hound", "--config", config, "run"]) == 0
    assert "no está en marcha" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--config", config, "status", "-m", "hound"])
