from pathlib import Path
import pytest
from optimizer_pipeline.backends.xilinx.configured_environment import configured_environment, load_tool_config


def test_explicit_tool_environment_does_not_run_any_tool(tmp_path):
    java = tmp_path / "java/bin/java"; java.parent.mkdir(parents=True)
    python, vivado = tmp_path / "python", tmp_path / "vivado"
    for p in (java, python, vivado):
        p.write_text("DO NOT EXECUTE"); p.chmod(0o700)
    rw = tmp_path / "rw"; (rw / "jars").mkdir(parents=True)
    (rw / "jars/fixture.jar").write_text("fixture")
    env = configured_environment(python=python, vivado=vivado, rapidwright=rw, java_home=java.parents[1])
    assert env["RAPIDWRIGHT_PATH"] == str(rw)
    assert "fixture.jar" in env["CLASSPATH"]
    assert env["VIVADO_EXEC"] == str(vivado)
    assert env["PYTHON"] == str(python.absolute())
    with pytest.raises(ValueError, match="Explicit"):
        configured_environment(python=python, vivado=vivado, rapidwright=None, java_home=java.parents[1])


def test_python_symlink_does_not_escape_isolated_environment(tmp_path):
    java = tmp_path / "java/bin/java"; java.parent.mkdir(parents=True)
    vivado = tmp_path / "vivado"
    for path in (java, vivado):
        path.write_text("fixture"); path.chmod(0o700)
    system = tmp_path / "system-python"; system.write_text("fixture"); system.chmod(0o700)
    python = tmp_path / "smoke/bin/python"; python.parent.mkdir(parents=True); python.symlink_to(system)
    rw = tmp_path / "rw"; (rw / "jars").mkdir(parents=True); (rw / "jars/test.jar").write_text("fixture")
    env = configured_environment(python=python, vivado=vivado, rapidwright=rw, java_home=java.parents[1])
    assert env["PYTHON"] == str(python) and env["PYTHON"] != str(system)


def test_config_paths_are_relative_to_file_not_working_directory(tmp_path, monkeypatch):
    import json
    folder = tmp_path / "configuration"; folder.mkdir()
    config = folder / "tools.json"
    config.write_text(json.dumps(dict(python="../venv/bin/python", vivado="../tools/vivado",
        rapidwright="../tools/rw", java_home="../tools/java", condition="S0")))
    monkeypatch.chdir(tmp_path)
    paths = load_tool_config(config)
    assert set(paths) == {"python", "vivado", "rapidwright", "java_home"}
    assert paths["python"] == folder / "../venv/bin/python"


@pytest.mark.parametrize("payload", [[], {}, {"python": 42}, {"python": ""}])
def test_missing_or_wrong_tool_paths_stop_explicitly(tmp_path, payload):
    import json
    config = tmp_path / "tools.json"; config.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="configuration"):
        load_tool_config(config)
