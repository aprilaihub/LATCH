from optimizer_pipeline.backends.xilinx.toolchain import _rapidwright_java_version


def test_explicit_jar_entries_identify_actual_api_jar(tmp_path, monkeypatch):
    jar = tmp_path / "rapidwright-api-lib-2025.2.1.jar"
    jar.touch()
    documentation = tmp_path / "rapidwright-api-lib-2025.2.1-javadoc.jar"
    documentation.touch()
    monkeypatch.setenv("CLASSPATH", f"{documentation}:{jar}")
    assert _rapidwright_java_version() == {
        "source": str(jar), "version": "2025.2.1", "parsed": (2025, 2, 1)
    }


def test_wildcard_classpath_still_supported(tmp_path, monkeypatch):
    jar = tmp_path / "rapidwright-api-lib-2025.2.1.jar"
    jar.touch()
    monkeypatch.setenv("CLASSPATH", str(tmp_path / "*"))
    assert _rapidwright_java_version()["source"] == str(jar)


def test_unrelated_explicit_jar_does_not_select_neighbor(tmp_path, monkeypatch):
    (tmp_path / "rapidwright-api-lib-2025.2.1.jar").touch()
    unrelated = tmp_path / "guava-33.6.0-jre.jar"
    unrelated.touch()
    monkeypatch.setenv("CLASSPATH", str(unrelated))
    assert _rapidwright_java_version()["parsed"] is None


def test_empty_classpath_does_not_scan_current_directory(tmp_path, monkeypatch):
    (tmp_path / "rapidwright-api-lib-2025.2.1.jar").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLASSPATH", "")
    assert _rapidwright_java_version()["parsed"] is None
