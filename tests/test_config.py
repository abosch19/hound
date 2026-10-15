from hound.config import load_config


def test_config_con_bom(tmp_path):
    p = tmp_path / "config.toml"
    p.write_bytes("\ufeff[share]\npath = 'D:\\Compartido'\n[index]\nextensions = ['PDF']\n".encode("utf-8"))
    cfg = load_config(p)
    assert cfg.share.path == "D:\\Compartido"
    assert cfg.index.extensions == [".pdf"]


def test_file_url():
    from hound.config import Config
    from hound.paths import file_url, links

    cfg = Config()
    assert links("a.pdf", cfg) == {}  # sin UNC no hay enlace
    cfg.share.unc = r"\\SERVIDOR\Compartido"
    assert links("Obras/Presupuesto cocina #2.pdf", cfg) == {
        "file_url": "file://SERVIDOR/Compartido/Obras/Presupuesto%20cocina%20%232.pdf",
        "folder_url": "file://SERVIDOR/Compartido/Obras/",
    }
    assert file_url("", cfg, folder=True) == "file://SERVIDOR/Compartido/"
