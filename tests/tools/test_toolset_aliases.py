from toolsets import resolve_toolset, validate_toolset


def test_files_alias_resolves_to_file_toolset():
    assert validate_toolset("files") is True
    assert resolve_toolset("files") == resolve_toolset("file")
