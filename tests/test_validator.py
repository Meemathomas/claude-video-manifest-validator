"""Unit tests for source/validator.py.

Total: 89 tests (81 test functions; 2 are parametrized into 4 and 6 cases).

    Group                      Tests  What it covers
    -------------------------  -----  -----------------------------------
    TestParseAttributeList         6  Attribute-list parsing
    TestUnquote                    3  Quoted-string helper
    TestCheckUriFormat            11  URI formatting rules
    TestValidManifest              5  Clean manifests and the bundled sample
    TestHeader                     7  #EXTM3U header and file reading
    TestVersion                    3  #EXT-X-VERSION
    TestStreamInf                 14  #EXT-X-STREAM-INF
    TestIFrameStreamInf            4  #EXT-X-I-FRAME-STREAM-INF
    TestMedia                     11  #EXT-X-MEDIA
    TestGroupReferences            4  AUDIO/VIDEO/SUBTITLES/CC group links
    TestSyntax                     4  Misplaced, unknown and stray lines
    TestLocalUriExistence          3  URI existence on disk
    TestRemoteUriExistence         4  URI existence over HTTP (mocked)
    TestCheckRemoteUri             4  HTTP probing helper (mocked)
    TestReport                     3  Printed validation report
    TestMain                       3  Command line and exit codes

Network access is mocked, so the suite runs offline.
Run with:  python -m pytest
"""

import sys
import urllib.error

import pytest

import validator
from validator import (
    DEFAULT_MANIFEST,
    ERROR,
    WARNING,
    ManifestValidator,
    check_remote_uri,
    check_uri_format,
    parse_attribute_list,
    print_report,
    unquote,
)

# A small master playlist that passes every check when its URIs exist.
VALID_MASTER = """\
#EXTM3U
#EXT-X-VERSION:6
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,\
AUTOSELECT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="sub",NAME="English",URI="subs/en.m3u8"
#EXT-X-MEDIA:TYPE=CLOSED-CAPTIONS,GROUP-ID="cc",NAME="English",\
INSTREAM-ID="CC1"
#EXT-X-STREAM-INF:BANDWIDTH=1280000,AVERAGE-BANDWIDTH=1000000,\
CODECS="avc1.640020,mp4a.40.2",RESOLUTION=1280x720,FRAME-RATE=30.000,\
AUDIO="aud",SUBTITLES="sub",CLOSED-CAPTIONS="cc"
video/720p.m3u8
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=90000,CODECS="avc1.640020",\
RESOLUTION=1280x720,URI="video/720p_iframe.m3u8"
"""

VALID_URIS = [
    "audio/en.m3u8",
    "subs/en.m3u8",
    "video/720p.m3u8",
    "video/720p_iframe.m3u8",
]

HEADER = "#EXTM3U\n#EXT-X-VERSION:6\n"
STREAM = '#EXT-X-STREAM-INF:BANDWIDTH=500000,CODECS="avc1.640020"\n'


def write_manifest(directory, text, uris=()):
    """Write a manifest plus empty files for the given relative URIs."""
    for uri in uris:
        child = directory / uri
        child.parent.mkdir(parents=True, exist_ok=True)
        child.write_text("#EXTM3U\n", encoding="utf-8")
    path = directory / "master.m3u8"
    path.write_text(text, encoding="utf-8")
    return path


def run_validator(directory, text, uris=(), base_url=None):
    path = write_manifest(directory, text, uris)
    return ManifestValidator(path, base_url).validate()


def find_issues(result, severity=None, category=None):
    return [issue for issue in result.issues
            if (severity is None or issue.severity == severity)
            and (category is None or issue.category == category)]


def has_issue(result, severity, category, text, line=None):
    return any(text in issue.message
               and (line is None or issue.line == line)
               for issue in find_issues(result, severity, category))


# --- parse_attribute_list -------------------------------------------------

class TestParseAttributeList:
    # Parses a mix of integer, quoted (containing a comma) and resolution
    # values into a name -> raw value dict with no problems reported.
    def test_parses_plain_and_quoted_values(self):
        attributes, problems = parse_attribute_list(
            'BANDWIDTH=100,CODECS="avc1,mp4a",RESOLUTION=640x360')
        assert problems == []
        assert attributes == {
            "BANDWIDTH": "100",
            "CODECS": '"avc1,mp4a"',
            "RESOLUTION": "640x360",
        }

    # An empty attribute list yields no attributes and no problems.
    def test_empty_list(self):
        assert parse_attribute_list("") == ({}, [])

    # The same attribute name appearing twice is reported as a duplicate.
    def test_duplicate_attribute(self):
        _, problems = parse_attribute_list("BANDWIDTH=1,BANDWIDTH=2")
        assert problems == ["duplicate attribute BANDWIDTH"]

    # A comma after the last attribute is reported as a trailing comma.
    def test_trailing_comma(self):
        _, problems = parse_attribute_list("BANDWIDTH=1,")
        assert problems == ["trailing comma in attribute list"]

    # Two attributes with no comma between them are reported as a missing
    # separator.
    def test_missing_comma_between_attributes(self):
        _, problems = parse_attribute_list('CODECS="avc1"BANDWIDTH=1')
        assert len(problems) == 1
        assert problems[0].startswith("expected ','")

    # Attribute names must be uppercase; a lowercase name is malformed.
    def test_lowercase_name_is_malformed(self):
        _, problems = parse_attribute_list("bandwidth=1")
        assert problems[0].startswith("malformed attribute list")


class TestUnquote:
    # Removes the surrounding double quotes from a quoted string.
    def test_strips_surrounding_quotes(self):
        assert unquote('"aud"') == "aud"

    # Returns an unquoted value such as NONE unchanged.
    def test_leaves_unquoted_value_alone(self):
        assert unquote("NONE") == "NONE"

    # A lone quote character is not a quoted string and is left unchanged.
    def test_leaves_single_quote_character_alone(self):
        assert unquote('"') == '"'


# --- check_uri_format -----------------------------------------------------

class TestCheckUriFormat:
    # Relative paths, bare file names, and http/https URLs (with or without
    # a query string) produce no formatting problems. Runs once per URI.
    @pytest.mark.parametrize("uri", [
        "v1/prog_index.m3u8",
        "prog_index.m3u8",
        "https://example.com/hls/v1.m3u8",
        "http://example.com/v1.m3u8?token=abc",
    ])
    def test_valid_uris(self, uri):
        assert check_uri_format(uri) == []

    # Each malformed URI (empty, whitespace, backslash, unsupported scheme,
    # missing host, directory instead of file) is reported as an ERROR with
    # the expected message. Runs once per case.
    @pytest.mark.parametrize("uri, expected", [
        ("", "URI is empty"),
        ("my video.m3u8", "contains whitespace"),
        ("v1\\prog.m3u8", "contains a backslash"),
        ("ftp://example.com/v1.m3u8", "unsupported scheme 'ftp'"),
        ("http:///v1.m3u8", "has no host"),
        ("video/", "does not name a file"),
    ])
    def test_invalid_uris_are_errors(self, uri, expected):
        problems = check_uri_format(uri)
        assert any(severity == ERROR and expected in message
                   for severity, message in problems)

    # A URI that doesn't end in .m3u8 is only a WARNING, not an error.
    def test_non_m3u8_extension_is_a_warning(self):
        assert check_uri_format("video/720p.mp4") == [
            (WARNING, "URI 'video/720p.mp4' does not end in .m3u8")]


# --- Whole manifests --------------------------------------------------------

class TestValidManifest:
    # A fully valid manifest whose URIs all exist reports zero issues.
    def test_valid_manifest_has_no_issues(self, tmp_path):
        result = run_validator(tmp_path, VALID_MASTER, VALID_URIS)
        assert result.issues == []
        assert result.error_count == 0
        assert result.warning_count == 0

    # The result records the version, the count of each tag type, every
    # unique URI, and a "found" status for each URI.
    def test_collects_counts_and_version(self, tmp_path):
        result = run_validator(tmp_path, VALID_MASTER, VALID_URIS)
        assert result.version == 6
        assert result.variant_count == 1
        assert result.iframe_count == 1
        assert result.media_count == 3
        assert sorted(result.uris) == sorted(VALID_URIS)
        assert set(result.uri_status.values()) == {"found (local file)"}

    # Windows (CRLF) line endings are accepted just like LF.
    def test_crlf_line_endings_are_accepted(self, tmp_path):
        path = write_manifest(tmp_path, "", VALID_URIS)
        path.write_bytes(VALID_MASTER.replace("\n", "\r\n").encode("utf-8"))
        result = ManifestValidator(path).validate()
        assert result.issues == []

    # Plain "#" comment lines are ignored and don't trigger warnings.
    def test_comments_are_ignored(self, tmp_path):
        text = VALID_MASTER.replace(
            "#EXT-X-INDEPENDENT-SEGMENTS",
            "#EXT-X-INDEPENDENT-SEGMENTS\n# just a comment")
        result = run_validator(tmp_path, text, VALID_URIS)
        assert result.issues == []

    # The bundled Apple sample validates with no errors and has the
    # expected version and tag/URI counts.
    def test_bundled_sample_has_no_errors(self):
        result = ManifestValidator(DEFAULT_MANIFEST).validate()
        assert result.error_count == 0
        assert result.version == 6
        assert result.variant_count == 24
        assert result.iframe_count == 6
        assert result.media_count == 5
        assert len(result.uris) == 18


class TestHeader:
    # A file whose first line isn't #EXTM3U is an error on line 1.
    def test_missing_extm3u(self, tmp_path):
        result = run_validator(tmp_path, "#EXT-X-VERSION:6\n")
        assert has_issue(result, ERROR, "header",
                         "first line must be #EXTM3U", line=1)

    # An empty file is reported as an error.
    def test_empty_file(self, tmp_path):
        result = run_validator(tmp_path, "")
        assert has_issue(result, ERROR, "header", "file is empty")

    # A byte-order mark before #EXTM3U is reported as an error.
    def test_byte_order_mark(self, tmp_path):
        result = run_validator(tmp_path, "﻿#EXTM3U\n")
        assert has_issue(result, ERROR, "header", "byte-order mark")

    # Whitespace around the #EXTM3U tag is reported as an error.
    def test_whitespace_around_extm3u(self, tmp_path):
        result = run_validator(tmp_path, "  #EXTM3U\n")
        assert has_issue(result, ERROR, "header", "surrounding whitespace")

    # A second #EXTM3U tag later in the file is an error on that line.
    def test_duplicate_extm3u(self, tmp_path):
        result = run_validator(tmp_path, "#EXTM3U\n#EXTM3U\n")
        assert has_issue(result, ERROR, "header", "more than once", line=2)

    # A path that doesn't exist is reported as "file not found" instead of
    # raising an exception.
    def test_missing_file(self, tmp_path):
        result = ManifestValidator(tmp_path / "nope.m3u8").validate()
        assert has_issue(result, ERROR, "header", "file not found")

    # A file containing bytes that aren't valid UTF-8 is reported as an
    # error instead of raising an exception.
    def test_non_utf8_file(self, tmp_path):
        path = tmp_path / "master.m3u8"
        path.write_bytes(b"#EXTM3U\n\xff\xfe\n")
        result = ManifestValidator(path).validate()
        assert has_issue(result, ERROR, "header", "not valid UTF-8")


class TestVersion:
    # A non-numeric EXT-X-VERSION is an error and no version is recorded.
    def test_non_integer_version(self, tmp_path):
        result = run_validator(tmp_path, "#EXTM3U\n#EXT-X-VERSION:abc\n")
        assert has_issue(result, ERROR, "version", "positive integer")
        assert result.version is None

    # EXT-X-VERSION:0 is an error because the version must be 1 or higher.
    def test_zero_version(self, tmp_path):
        result = run_validator(tmp_path, "#EXTM3U\n#EXT-X-VERSION:0\n")
        assert has_issue(result, ERROR, "version", "positive integer")

    # A second EXT-X-VERSION tag is an error that points back to the first
    # one, and the first value is the one kept.
    def test_duplicate_version(self, tmp_path):
        result = run_validator(
            tmp_path, "#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-VERSION:7\n")
        assert has_issue(result, ERROR, "version",
                         "more than once (first on line 2)", line=3)
        assert result.version == 6


class TestStreamInf:
    def check(self, tmp_path, attributes, uri="v.m3u8"):
        text = f"{HEADER}#EXT-X-STREAM-INF:{attributes}\n{uri}\n"
        return run_validator(tmp_path, text, [uri])

    # BANDWIDTH is required; leaving it out is an error.
    def test_missing_bandwidth(self, tmp_path):
        result = self.check(tmp_path, 'CODECS="avc1.640020"')
        assert has_issue(result, ERROR, "stream-inf",
                         "missing required attribute BANDWIDTH")

    # BANDWIDTH=0 is an error because it must be greater than 0.
    def test_zero_bandwidth(self, tmp_path):
        result = self.check(tmp_path, 'BANDWIDTH=0,CODECS="avc1"')
        assert has_issue(result, ERROR, "stream-inf",
                         "BANDWIDTH must be greater than 0")

    # A decimal BANDWIDTH such as 1.5 is an error because it must be an
    # integer.
    def test_non_integer_bandwidth(self, tmp_path):
        result = self.check(tmp_path, 'BANDWIDTH=1.5,CODECS="avc1"')
        assert has_issue(result, ERROR, "stream-inf",
                         "BANDWIDTH=1.5 should be a decimal integer")

    # AVERAGE-BANDWIDTH higher than peak BANDWIDTH is a WARNING only.
    def test_average_above_peak_is_warning(self, tmp_path):
        result = self.check(
            tmp_path, 'BANDWIDTH=100,AVERAGE-BANDWIDTH=200,CODECS="avc1"')
        assert has_issue(result, WARNING, "stream-inf",
                         "AVERAGE-BANDWIDTH (200) exceeds peak")
        assert result.error_count == 0

    # A RESOLUTION not in WIDTHxHEIGHT form (e.g. 640by360) is an error.
    def test_malformed_resolution(self, tmp_path):
        result = self.check(
            tmp_path, 'BANDWIDTH=1,CODECS="avc1",RESOLUTION=640by360')
        assert has_issue(result, ERROR, "stream-inf",
                         "RESOLUTION=640by360 should be WIDTHxHEIGHT")

    # A RESOLUTION with a zero width or height (1280x0) is an error.
    def test_zero_resolution_dimension(self, tmp_path):
        result = self.check(
            tmp_path, 'BANDWIDTH=1,CODECS="avc1",RESOLUTION=1280x0')
        assert has_issue(result, ERROR, "stream-inf", "zero dimension")

    # CODECS must be a quoted string; an unquoted value is an error.
    def test_unquoted_codecs(self, tmp_path):
        result = self.check(tmp_path, "BANDWIDTH=1,CODECS=avc1")
        assert has_issue(result, ERROR, "stream-inf",
                         "CODECS=avc1 should be a quoted string")

    # A non-numeric FRAME-RATE is an error.
    def test_malformed_frame_rate(self, tmp_path):
        result = self.check(
            tmp_path, 'BANDWIDTH=1,CODECS="avc1",FRAME-RATE=fast')
        assert has_issue(result, ERROR, "stream-inf",
                         "FRAME-RATE=fast should be a decimal number")

    # Leaving out CODECS is a WARNING only, because the spec says SHOULD.
    def test_missing_codecs_is_warning(self, tmp_path):
        result = self.check(tmp_path, "BANDWIDTH=1")
        assert has_issue(result, WARNING, "stream-inf",
                         "should include a CODECS attribute")
        assert result.error_count == 0

    # An attribute the validator doesn't know (FOO) is a WARNING.
    def test_unknown_attribute_is_warning(self, tmp_path):
        result = self.check(tmp_path, 'BANDWIDTH=1,CODECS="avc1",FOO=1')
        assert has_issue(result, WARNING, "stream-inf",
                         "unrecognised attribute FOO")

    # CLOSED-CAPTIONS=NONE (unquoted) is valid and needs no matching group.
    def test_closed_captions_none_is_allowed(self, tmp_path):
        result = self.check(
            tmp_path, 'BANDWIDTH=1,CODECS="avc1",CLOSED-CAPTIONS=NONE')
        assert result.issues == []

    # A STREAM-INF followed directly by another tag, rather than a URI
    # line, is an error on the first STREAM-INF's line.
    def test_followed_by_another_tag_instead_of_uri(self, tmp_path):
        text = f"{HEADER}{STREAM}{STREAM}v.m3u8\n"
        result = run_validator(tmp_path, text, ["v.m3u8"])
        assert has_issue(result, ERROR, "stream-inf",
                         "not followed by a URI line", line=3)

    # A STREAM-INF on the last line of the file, with no URI after it, is
    # an error.
    def test_stream_inf_at_end_of_file(self, tmp_path):
        result = run_validator(tmp_path, f"{HEADER}{STREAM}")
        assert has_issue(result, ERROR, "stream-inf",
                         "not followed by a URI line", line=3)

    # A blank line between a STREAM-INF and its URI is allowed.
    def test_blank_line_before_uri_is_allowed(self, tmp_path):
        result = run_validator(
            tmp_path, f"{HEADER}{STREAM}\nv.m3u8\n", ["v.m3u8"])
        assert result.issues == []


class TestIFrameStreamInf:
    # URI is required on I-frame streams; leaving it out is an error.
    def test_missing_uri(self, tmp_path):
        text = HEADER + '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1000\n'
        result = run_validator(tmp_path, text)
        assert has_issue(result, ERROR, "i-frame",
                         "missing required attribute URI")

    # BANDWIDTH is required on I-frame streams; leaving it out is an error.
    def test_missing_bandwidth(self, tmp_path):
        text = HEADER + '#EXT-X-I-FRAME-STREAM-INF:URI="i.m3u8"\n'
        result = run_validator(tmp_path, text, ["i.m3u8"])
        assert has_issue(result, ERROR, "i-frame",
                         "missing required attribute BANDWIDTH")

    # The URI attribute is unquoted and recorded with its line number, and
    # a valid tag reports no issues.
    def test_uri_attribute_is_recorded(self, tmp_path):
        text = HEADER + ('#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1000,'
                         'URI="iframes/i.m3u8"\n')
        result = run_validator(tmp_path, text, ["iframes/i.m3u8"])
        assert result.issues == []
        assert result.uris == {"iframes/i.m3u8": 3}

    # The URI attribute must be a quoted string; an unquoted value is an
    # error.
    def test_unquoted_uri(self, tmp_path):
        text = HEADER + ('#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1000,'
                         'URI=i.m3u8\n')
        result = run_validator(tmp_path, text, ["i.m3u8"])
        assert has_issue(result, ERROR, "i-frame",
                         "URI=i.m3u8 should be a quoted string")


class TestMedia:
    def check(self, tmp_path, attributes, uris=()):
        return run_validator(
            tmp_path, f"{HEADER}#EXT-X-MEDIA:{attributes}\n", uris)

    # TYPE, GROUP-ID and NAME are all required; each missing one is
    # reported as a separate error.
    def test_missing_required_attributes(self, tmp_path):
        result = self.check(tmp_path, 'LANGUAGE="en"')
        for name in ("TYPE", "GROUP-ID", "NAME"):
            assert has_issue(result, ERROR, "media",
                             f"missing required attribute {name}")

    # A TYPE other than AUDIO/VIDEO/SUBTITLES/CLOSED-CAPTIONS is an error.
    def test_invalid_type(self, tmp_path):
        result = self.check(tmp_path, 'TYPE=MUSIC,GROUP-ID="a",NAME="A"')
        assert has_issue(result, ERROR, "media", "TYPE=MUSIC must be one of")

    # A CLOSED-CAPTIONS rendition with a URI is an error, because captions
    # are carried inside the video stream.
    def test_closed_captions_with_uri(self, tmp_path):
        result = self.check(
            tmp_path, 'TYPE=CLOSED-CAPTIONS,GROUP-ID="cc",NAME="E",'
                      'INSTREAM-ID="CC1",URI="cc.m3u8"', ["cc.m3u8"])
        assert has_issue(result, ERROR, "media",
                         "CLOSED-CAPTIONS must not have a URI")

    # A CLOSED-CAPTIONS rendition without INSTREAM-ID is an error.
    def test_closed_captions_without_instream_id(self, tmp_path):
        result = self.check(
            tmp_path, 'TYPE=CLOSED-CAPTIONS,GROUP-ID="cc",NAME="E"')
        assert has_issue(result, ERROR, "media",
                         "CLOSED-CAPTIONS requires INSTREAM-ID")

    # INSTREAM-ID on a non-caption rendition (AUDIO) is an error.
    def test_instream_id_on_audio(self, tmp_path):
        result = self.check(
            tmp_path, 'TYPE=AUDIO,GROUP-ID="a",NAME="E",INSTREAM-ID="CC1"')
        assert has_issue(result, ERROR, "media",
                         "INSTREAM-ID is only allowed")

    # A SUBTITLES rendition without a URI is an error.
    def test_subtitles_without_uri(self, tmp_path):
        result = self.check(tmp_path, 'TYPE=SUBTITLES,GROUP-ID="s",NAME="E"')
        assert has_issue(result, ERROR, "media", "SUBTITLES requires a URI")

    # FORCED on a non-subtitle rendition (AUDIO) is an error.
    def test_forced_on_audio(self, tmp_path):
        result = self.check(
            tmp_path, 'TYPE=AUDIO,GROUP-ID="a",NAME="E",FORCED=NO')
        assert has_issue(result, ERROR, "media", "FORCED is only allowed")

    # DEFAULT=YES together with AUTOSELECT=NO is an error.
    def test_default_yes_with_autoselect_no(self, tmp_path):
        result = self.check(
            tmp_path, 'TYPE=AUDIO,GROUP-ID="a",NAME="E",DEFAULT=YES,'
                      'AUTOSELECT=NO')
        assert has_issue(result, ERROR, "media",
                         "AUTOSELECT must be YES when DEFAULT=YES")

    # A YES/NO attribute with any other value (DEFAULT=TRUE) is an error.
    def test_invalid_yes_no_value(self, tmp_path):
        result = self.check(
            tmp_path, 'TYPE=AUDIO,GROUP-ID="a",NAME="E",DEFAULT=TRUE')
        assert has_issue(result, ERROR, "media",
                         "DEFAULT=TRUE should be YES or NO")

    # Two renditions with the same NAME in the same group is an error on
    # the second one.
    def test_duplicate_name_in_group(self, tmp_path):
        text = (HEADER
                + '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English"\n'
                + '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English"\n')
        result = run_validator(tmp_path, text)
        assert has_issue(result, ERROR, "media",
                         'NAME "English" is used twice', line=4)

    # The same NAME in two different groups is allowed.
    def test_same_name_in_different_groups_is_allowed(self, tmp_path):
        text = (HEADER
                + '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English"\n'
                + '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="b",NAME="English"\n')
        result = run_validator(tmp_path, text)
        assert find_issues(result, ERROR, "media") == []


class TestGroupReferences:
    # A variant's AUDIO group that no EXT-X-MEDIA tag declares is an error
    # on the variant's line.
    def test_undeclared_group(self, tmp_path):
        text = (HEADER + '#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1",'
                'AUDIO="missing"\nv.m3u8\n')
        result = run_validator(tmp_path, text, ["v.m3u8"])
        assert has_issue(result, ERROR, "group-refs",
                         'AUDIO="missing" has no matching', line=3)

    # AUDIO pointing at a group declared as SUBTITLES is an error, because
    # the group type must match the attribute.
    def test_group_of_wrong_type(self, tmp_path):
        text = (HEADER
                + '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="g",NAME="E",'
                  'URI="s.m3u8"\n'
                + '#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1",'
                  'AUDIO="g"\nv.m3u8\n')
        result = run_validator(tmp_path, text, ["s.m3u8", "v.m3u8"])
        assert has_issue(result, ERROR, "group-refs",
                         "no matching EXT-X-MEDIA TYPE=AUDIO group")

    # A declared group that no variant references is a WARNING.
    def test_unreferenced_group_is_warning(self, tmp_path):
        text = HEADER + '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="E"\n'
        result = run_validator(tmp_path, text)
        assert has_issue(result, WARNING, "group-refs",
                         'AUDIO group "a" is never referenced')

    # An I-frame stream's VIDEO group reference is also checked; an
    # undeclared group is an error.
    def test_iframe_video_reference(self, tmp_path):
        text = (HEADER + '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,'
                'VIDEO="cam",URI="i.m3u8"\n')
        result = run_validator(tmp_path, text, ["i.m3u8"])
        assert has_issue(result, ERROR, "group-refs",
                         'VIDEO="cam" has no matching')


class TestSyntax:
    # A media-playlist-only tag (#EXTINF) in a master playlist is an error.
    def test_media_playlist_tag(self, tmp_path):
        result = run_validator(tmp_path, HEADER + "#EXTINF:6.0,\n")
        assert has_issue(result, ERROR, "syntax",
                         "#EXTINF is a media playlist tag", line=3)

    # An unknown #EXT tag is a WARNING, not an error.
    def test_unknown_tag_is_warning(self, tmp_path):
        result = run_validator(tmp_path, HEADER + "#EXT-X-MADE-UP:1\n")
        assert has_issue(result, WARNING, "syntax",
                         "unrecognised tag #EXT-X-MADE-UP")

    # A URI line with no EXT-X-STREAM-INF tag before it is an error.
    def test_uri_without_stream_inf(self, tmp_path):
        result = run_validator(tmp_path, HEADER + "orphan.m3u8\n")
        assert has_issue(result, ERROR, "syntax",
                         "has no preceding EXT-X-STREAM-INF", line=3)

    # Trailing whitespace on a line is a WARNING.
    def test_trailing_whitespace_is_warning(self, tmp_path):
        result = run_validator(
            tmp_path, "#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS   \n")
        assert has_issue(result, WARNING, "syntax",
                         "leading or trailing whitespace", line=2)


# --- URI existence ----------------------------------------------------------

class TestLocalUriExistence:
    # When none of the URIs exist on disk, a single summary WARNING is
    # reported instead of one warning per URI.
    def test_all_missing_gives_single_warning(self, tmp_path):
        result = run_validator(tmp_path, VALID_MASTER)
        warnings = find_issues(result, WARNING, "uri-exists")
        assert len(warnings) == 1
        assert "none of the 4 URIs exist" in warnings[0].message
        assert result.error_count == 0

    # When only some URIs are missing, each missing URI gets its own
    # WARNING, and the found and missing statuses are recorded correctly.
    def test_some_missing_gives_warning_per_uri(self, tmp_path):
        result = run_validator(tmp_path, VALID_MASTER, VALID_URIS[:2])
        warnings = find_issues(result, WARNING, "uri-exists")
        assert len(warnings) == 2
        assert result.uri_status["video/720p.m3u8"] == "not found locally"
        assert result.uri_status["audio/en.m3u8"] == "found (local file)"

    # A badly formatted URI is marked "skipped" and not looked up on disk.
    def test_badly_formatted_uri_is_not_checked(self, tmp_path):
        text = f"{HEADER}{STREAM}bad name.m3u8\n"
        result = run_validator(tmp_path, text)
        assert result.uri_status["bad name.m3u8"] == "skipped (bad format)"
        assert find_issues(result, category="uri-exists") == []


class TestRemoteUriExistence:
    BASE_URL = "https://cdn.example.com/hls/master.m3u8"

    @pytest.fixture
    def fake_remote(self, monkeypatch):
        """Replace check_remote_uri; returns the list of URLs probed."""
        probed = []
        responses = {}

        def fake_check(url):
            probed.append(url)
            return responses.get(url, (True, "HTTP 200"))

        monkeypatch.setattr(validator, "check_remote_uri", fake_check)
        return probed, responses

    # With --base-url, each relative URI is resolved against the base URL's
    # folder before it's checked, and URIs that respond are marked found.
    def test_uris_resolved_against_base_url(self, tmp_path, fake_remote):
        probed, _ = fake_remote
        result = run_validator(tmp_path, VALID_MASTER,
                               base_url=self.BASE_URL)
        assert sorted(probed) == sorted(
            f"https://cdn.example.com/hls/{uri}" for uri in VALID_URIS)
        assert result.issues == []
        assert set(result.uri_status.values()) == {"found (HTTP 200)"}

    # A URI that returns HTTP 404 from the server is an ERROR, and its
    # status shows the HTTP code.
    def test_missing_remote_uri_is_error(self, tmp_path, fake_remote):
        _, responses = fake_remote
        missing = "https://cdn.example.com/hls/video/720p.m3u8"
        responses[missing] = (False, "HTTP 404")
        result = run_validator(tmp_path, VALID_MASTER,
                               base_url=self.BASE_URL)
        assert has_issue(result, ERROR, "uri-exists",
                         f"{missing} returned HTTP 404")
        assert result.uri_status["video/720p.m3u8"] == "missing (HTTP 404)"

    # A server that can't be reached is only a WARNING, because the URI's
    # existence is unknown rather than disproved.
    def test_unreachable_server_is_warning(self, tmp_path, fake_remote):
        _, responses = fake_remote
        url = "https://cdn.example.com/hls/audio/en.m3u8"
        responses[url] = (None, "unreachable (timed out)")
        result = run_validator(tmp_path, VALID_MASTER,
                               base_url=self.BASE_URL)
        assert has_issue(result, WARNING, "uri-exists", "could not check")
        assert result.uri_status["audio/en.m3u8"].startswith("unknown")
        assert result.error_count == 0

    # An absolute http(s) URI is checked over the network as-is, even
    # without --base-url.
    def test_absolute_uri_checked_without_base_url(self, tmp_path,
                                                   fake_remote):
        probed, _ = fake_remote
        url = "https://other.example.com/v.m3u8"
        run_validator(tmp_path, f"{HEADER}{STREAM}{url}\n")
        assert probed == [url]


class TestCheckRemoteUri:
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    @staticmethod
    def http_error(code):
        return urllib.error.HTTPError("https://x", code, "error", {}, None)

    # A successful response returns (True, "HTTP 200").
    def test_found(self, monkeypatch):
        monkeypatch.setattr(validator.urllib.request, "urlopen",
                            lambda request, timeout: self.FakeResponse())
        assert check_remote_uri("https://x/v.m3u8") == (True, "HTTP 200")

    # An HTTP 404 response returns (False, "HTTP 404").
    def test_not_found(self, monkeypatch):
        def raise_404(request, timeout):
            raise self.http_error(404)

        monkeypatch.setattr(validator.urllib.request, "urlopen", raise_404)
        assert check_remote_uri("https://x/v.m3u8") == (False, "HTTP 404")

    # When a server rejects HEAD with 405, the request is retried with GET,
    # and the GET result is used.
    def test_head_rejected_falls_back_to_get(self, monkeypatch):
        methods = []

        def fake_urlopen(request, timeout):
            methods.append(request.get_method())
            if request.get_method() == "HEAD":
                raise self.http_error(405)
            return self.FakeResponse()

        monkeypatch.setattr(validator.urllib.request, "urlopen", fake_urlopen)
        assert check_remote_uri("https://x/v.m3u8") == (True, "HTTP 200")
        assert methods == ["HEAD", "GET"]

    # A network failure returns None (unknown) with the reason included in
    # the detail text.
    def test_unreachable(self, monkeypatch):
        def raise_url_error(request, timeout):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(validator.urllib.request, "urlopen",
                            raise_url_error)
        exists, detail = check_remote_uri("https://x/v.m3u8")
        assert exists is None
        assert "no route to host" in detail


# --- Report and command line --------------------------------------------

class TestReport:
    # The report for a valid manifest shows the title and version, no FAIL
    # or WARN lines, and a "PASSED (0 errors, 0 warnings)" result.
    def test_passing_report(self, tmp_path, capsys):
        result = run_validator(tmp_path, VALID_MASTER, VALID_URIS)
        print_report(result)
        output = capsys.readouterr().out
        assert "HLS Manifest Validation Report" in output
        assert "Version:   6" in output
        assert "[FAIL]" not in output
        assert "[WARN]" not in output
        assert "Result: PASSED (0 errors, 0 warnings)" in output

    # The report for an invalid manifest shows a FAIL check line, the error
    # with its line number, and a FAILED result with a singular "error".
    def test_failing_report(self, tmp_path, capsys):
        result = run_validator(tmp_path, "#EXT-X-VERSION:6\n")
        print_report(result)
        output = capsys.readouterr().out
        assert "[FAIL] File starts with #EXTM3U" in output
        assert "ERROR    line 1" in output
        assert "Result: FAILED (1 error, 0 warnings)" in output

    # A manifest with only warnings shows a WARN check line and a
    # "PASSED with warnings" result.
    def test_warning_report(self, tmp_path, capsys):
        result = run_validator(tmp_path, VALID_MASTER)
        print_report(result)
        output = capsys.readouterr().out
        assert "[WARN] URIs exist" in output
        assert "Result: PASSED with warnings (0 errors, 1 warning)" in output


class TestMain:
    def run_main(self, monkeypatch, *args):
        monkeypatch.setattr(sys, "argv", ["validator.py", *args])
        return validator.main()

    # Running the command on a valid manifest exits with code 0 and prints
    # a PASSED result.
    def test_exit_code_zero_for_valid_manifest(self, tmp_path, monkeypatch,
                                               capsys):
        path = write_manifest(tmp_path, VALID_MASTER, VALID_URIS)
        assert self.run_main(monkeypatch, str(path)) == 0
        assert "Result: PASSED" in capsys.readouterr().out

    # Running the command on an invalid manifest exits with code 1 and
    # prints a FAILED result.
    def test_exit_code_one_for_invalid_manifest(self, tmp_path, monkeypatch,
                                                capsys):
        path = write_manifest(tmp_path, "not a playlist\n")
        assert self.run_main(monkeypatch, str(path)) == 1
        assert "Result: FAILED" in capsys.readouterr().out

    # Running the command with no arguments validates the bundled sample
    # (samples/master_manifest/sample_master1.m3u8) and exits with code 0.
    def test_defaults_to_bundled_sample(self, monkeypatch, capsys):
        assert self.run_main(monkeypatch) == 0
        assert "sample_master1.m3u8" in capsys.readouterr().out
