from hound.config import load_config


def test_config_con_bom(tmp_path):
    p = tmp_path / "config.toml"
    p.write_bytes("\ufeff[share]\npath = 'D:\\Compartido'\n[index]\nextensions = ['PDF']\n".encode("utf-8"))
    cfg = load_config(p)
    assert cfg.share.path == "D:\\Compartido"
    assert cfg.index.extensions == [".pdf"]
