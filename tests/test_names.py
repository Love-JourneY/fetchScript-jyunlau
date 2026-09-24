from fetchscript.names import normalize_existing_name, safe_title


def test_safe_title_keeps_chinese_and_fullwidth():
    assert safe_title("【原创】深夜发疯 3.0｜vlog") == "【原创】深夜发疯 3.0｜vlog"


def test_safe_title_replaces_path_separators():
    assert "/" not in safe_title("a/b:c*d?e")
    assert "／" in safe_title("a/b")


def test_safe_title_collapses_whitespace_and_trims():
    assert safe_title("  多余   空格  ") == "多余 空格"
    assert safe_title("标题。") == "标题"


def test_safe_title_falls_back_when_empty():
    assert safe_title("   ") == "media"
    assert safe_title("", fallback="无标题") == "无标题"


def test_safe_title_truncates():
    assert len(safe_title("字" * 200)) == 80


def test_normalize_existing_name_keeps_suffix():
    assert normalize_existing_name("video.mp4") == "video.mp4"
    assert normalize_existing_name("  a  b .mp4") == "a b.mp4"
