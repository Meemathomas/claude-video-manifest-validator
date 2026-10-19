"""Validate an HLS master playlist (.m3u8) and print a validation report.

Checks performed (RFC 8216 / HLS spec):
  * The file starts with the #EXTM3U tag.
  * EXT-X-VERSION, EXT-X-STREAM-INF, EXT-X-I-FRAME-STREAM-INF and
    EXT-X-MEDIA tags have well-formed attribute lists with valid values.
  * Every EXT-X-STREAM-INF tag is followed by a URI line.
  * AUDIO / VIDEO / SUBTITLES / CLOSED-CAPTIONS group references point at
    a declared EXT-X-MEDIA group.
  * Every URI is correctly formatted and exists, either on disk next to the
    manifest or, with --base-url, on the server the manifest came from.

Usage:
    python source/validator.py [MANIFEST] [--base-url URL]

MANIFEST defaults to samples/master_manifest/sample_master1.m3u8.
"""

import argparse
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = (PROJECT_ROOT / "samples" / "master_manifest"
                    / "sample_master1.m3u8")

ERROR = "ERROR"
WARNING = "WARNING"

# Report sections, in display order: (category key, description).
CATEGORIES = [
    ("header", "File starts with #EXTM3U"),
    ("syntax", "Lines and tags are well placed"),
    ("version", "EXT-X-VERSION is valid"),
    ("stream-inf", "EXT-X-STREAM-INF tags are well formed"),
    ("i-frame", "EXT-X-I-FRAME-STREAM-INF tags are well formed"),
    ("media", "EXT-X-MEDIA tags are well formed"),
    ("group-refs", "Rendition group references resolve"),
    ("uri-format", "URIs are correctly formatted"),
    ("uri-exists", "URIs exist"),
]

# Attribute value types from RFC 8216 section 4.2.
VALUE_TYPES = {
    "integer": (re.compile(r"\d+"), "a decimal integer"),
    "float": (re.compile(r"\d+(\.\d+)?"), "a decimal number"),
    "resolution": (re.compile(r"\d+x\d+"), "WIDTHxHEIGHT"),
    "quoted": (re.compile(r'"[^"\r\n]*"'), "a quoted string"),
    "quoted-or-none": (re.compile(r'"[^"\r\n]*"|NONE'),
                       "a quoted string or NONE"),
    "enum": (re.compile(r'[^",\s]+'), "an unquoted keyword"),
    "yes-no": (re.compile(r"YES|NO"), "YES or NO"),
}

STREAM_INF_ATTRIBUTES = {
    "BANDWIDTH": "integer",
    "AVERAGE-BANDWIDTH": "integer",
    "SCORE": "float",
    "CODECS": "quoted",
    "SUPPLEMENTAL-CODECS": "quoted",
    "RESOLUTION": "resolution",
    "FRAME-RATE": "float",
    "HDCP-LEVEL": "enum",
    "ALLOWED-CPC": "quoted",
    "VIDEO-RANGE": "enum",
    "STABLE-VARIANT-ID": "quoted",
    "PATHWAY-ID": "quoted",
    "AUDIO": "quoted",
    "VIDEO": "quoted",
    "SUBTITLES": "quoted",
    "CLOSED-CAPTIONS": "quoted-or-none",
}

I_FRAME_ATTRIBUTES = {
    "BANDWIDTH": "integer",
    "AVERAGE-BANDWIDTH": "integer",
    "SCORE": "float",
    "CODECS": "quoted",
    "SUPPLEMENTAL-CODECS": "quoted",
    "RESOLUTION": "resolution",
    "HDCP-LEVEL": "enum",
    "ALLOWED-CPC": "quoted",
    "VIDEO-RANGE": "enum",
    "STABLE-VARIANT-ID": "quoted",
    "PATHWAY-ID": "quoted",
    "VIDEO": "quoted",
    "URI": "quoted",
}

MEDIA_ATTRIBUTES = {
    "TYPE": "enum",
    "URI": "quoted",
    "GROUP-ID": "quoted",
    "LANGUAGE": "quoted",
    "ASSOC-LANGUAGE": "quoted",
    "NAME": "quoted",
    "STABLE-RENDITION-ID": "quoted",
    "DEFAULT": "yes-no",
    "AUTOSELECT": "yes-no",
    "FORCED": "yes-no",
    "INSTREAM-ID": "quoted",
    "CHARACTERISTICS": "quoted",
    "CHANNELS": "quoted",
}

MEDIA_TYPES = {"AUDIO", "VIDEO", "SUBTITLES", "CLOSED-CAPTIONS"}

# Master playlist tags that need no checks beyond being recognised.
OTHER_MASTER_TAGS = {
    "#EXT-X-INDEPENDENT-SEGMENTS",
    "#EXT-X-START",
    "#EXT-X-DEFINE",
    "#EXT-X-SESSION-DATA",
    "#EXT-X-SESSION-KEY",
    "#EXT-X-CONTENT-STEERING",
}

# Tags that only belong in media playlists.
MEDIA_PLAYLIST_TAGS = {
    "#EXTINF",
    "#EXT-X-TARGETDURATION",
    "#EXT-X-MEDIA-SEQUENCE",
    "#EXT-X-DISCONTINUITY-SEQUENCE",
    "#EXT-X-ENDLIST",
    "#EXT-X-PLAYLIST-TYPE",
    "#EXT-X-I-FRAMES-ONLY",
    "#EXT-X-BYTERANGE",
    "#EXT-X-DISCONTINUITY",
    "#EXT-X-KEY",
    "#EXT-X-MAP",
    "#EXT-X-PROGRAM-DATE-TIME",
    "#EXT-X-GAP",
    "#EXT-X-PART",
}

ATTRIBUTE_PATTERN = re.compile(r'([A-Z0-9-]+)=("[^"\r\n]*"|[^",]*)')


@dataclass
class Issue:
    severity: str
    category: str
    line: int | None
    message: str


@dataclass
class ValidationResult:
    path: Path
    issues: list = field(default_factory=list)
    version: int | None = None
    variant_count: int = 0
    iframe_count: int = 0
    media_count: int = 0
    uris: dict = field(default_factory=dict)        # URI -> first line
    uri_status: dict = field(default_factory=dict)  # URI -> status text

    @property
    def error_count(self):
        return sum(1 for issue in self.issues if issue.severity == ERROR)

    @property
    def warning_count(self):
        return sum(1 for issue in self.issues if issue.severity == WARNING)


def parse_attribute_list(text):
    """Split an attribute list into a dict; return (attributes, problems)."""
    attributes = {}
    problems = []
    position = 0
    while position < len(text):
        match = ATTRIBUTE_PATTERN.match(text, position)
        if not match:
            snippet = text[position:position + 25]
            problems.append(f"malformed attribute list near '{snippet}'")
            break
        name, value = match.groups()
        if name in attributes:
            problems.append(f"duplicate attribute {name}")
        attributes[name] = value
        position = match.end()
        if position < len(text):
            if text[position] != ",":
                snippet = text[position:position + 25]
                problems.append(f"expected ',' before '{snippet}'")
                break
            position += 1
            if position == len(text):
                problems.append("trailing comma in attribute list")
    return attributes, problems


def unquote(value):
    """Strip the surrounding double quotes from a quoted-string value."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def check_uri_format(uri):
    """Return a list of (severity, message) formatting problems for a URI."""
    problems = []
    if not uri:
        return [(ERROR, "URI is empty")]
    if any(character.isspace() for character in uri):
        problems.append((ERROR, f"URI '{uri}' contains whitespace"))
    if "\\" in uri:
        problems.append(
            (ERROR, f"URI '{uri}' contains a backslash; use '/'"))
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme:
        if parsed.scheme not in ("http", "https"):
            problems.append(
                (ERROR, f"URI '{uri}' uses unsupported scheme "
                 f"'{parsed.scheme}'"))
        elif not parsed.netloc:
            problems.append((ERROR, f"URI '{uri}' has no host"))
    if not parsed.path or parsed.path.endswith("/"):
        problems.append((ERROR, f"URI '{uri}' does not name a file"))
    elif not parsed.path.lower().endswith(".m3u8"):
        problems.append(
            (WARNING, f"URI '{uri}' does not end in .m3u8"))
    return problems


def check_remote_uri(url):
    """Probe a URL; return (exists, detail) where exists may be None."""
    headers = {"User-Agent": "hls-manifest-validator/1.0"}
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return True, f"HTTP {response.status}"
        except urllib.error.HTTPError as error:
            # Some servers reject HEAD; retry those with GET.
            if method == "HEAD" and error.code in (403, 405, 501):
                continue
            return False, f"HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            return None, f"unreachable ({reason})"
    return None, "unreachable"


class ManifestValidator:
    """Validate one master playlist and collect the results."""

    def __init__(self, path, base_url=None):
        self.result = ValidationResult(Path(path))
        self.base_url = base_url
        self.media_groups = {}  # (TYPE, GROUP-ID) -> set of NAMEs
        self.group_refs = []    # (TYPE, GROUP-ID, line)
        self.version_line = None

    def add_issue(self, severity, category, line, message):
        self.result.issues.append(Issue(severity, category, line, message))

    def record_uri(self, uri, line):
        for severity, message in check_uri_format(uri):
            self.add_issue(severity, "uri-format", line, message)
        self.result.uris.setdefault(uri, line)

    def validate(self):
        try:
            text = self.result.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.add_issue(ERROR, "header", None,
                           f"file not found: {self.result.path}")
            return self.result
        except UnicodeDecodeError:
            self.add_issue(ERROR, "header", None,
                           "file is not valid UTF-8 text")
            return self.result

        lines = text.splitlines()
        header_ok = self.check_header(lines)
        self.check_lines(lines, skip_first=header_ok)
        self.check_group_refs()
        self.check_uris_exist()
        return self.result

    def check_header(self, lines):
        if not lines:
            self.add_issue(ERROR, "header", None, "file is empty")
            return False
        first = lines[0]
        if first.startswith("﻿"):
            self.add_issue(ERROR, "header", 1,
                           "file starts with a byte-order mark; "
                           "#EXTM3U must be the very first bytes")
            first = first.lstrip("﻿")
        if first == "#EXTM3U":
            return True
        if first.strip() == "#EXTM3U":
            self.add_issue(ERROR, "header", 1,
                           "#EXTM3U has surrounding whitespace")
            return True
        self.add_issue(ERROR, "header", 1,
                       f"first line must be #EXTM3U, found '{first[:40]}'")
        return False

    def check_lines(self, lines, skip_first):
        pending_stream_inf = None  # line of a STREAM-INF awaiting its URI
        for number, line in enumerate(lines, start=1):
            if number == 1 and skip_first:
                continue
            stripped = line.strip()
            if not stripped:
                continue
            if stripped != line:
                self.add_issue(WARNING, "syntax", number,
                               "line has leading or trailing whitespace")

            if stripped.startswith("#EXT"):
                if pending_stream_inf:
                    self.add_issue(ERROR, "stream-inf", pending_stream_inf,
                                   "EXT-X-STREAM-INF is not followed by "
                                   "a URI line")
                    pending_stream_inf = None
                tag, _, value = stripped.partition(":")
                if tag == "#EXT-X-STREAM-INF":
                    self.check_stream_inf(value, number)
                    pending_stream_inf = number
                else:
                    self.check_tag(tag, value, number)
            elif stripped.startswith("#"):
                continue  # comment
            elif pending_stream_inf:
                self.record_uri(stripped, number)
                pending_stream_inf = None
            else:
                self.add_issue(ERROR, "syntax", number,
                               f"URI line '{stripped}' has no preceding "
                               "EXT-X-STREAM-INF tag")

        if pending_stream_inf:
            self.add_issue(ERROR, "stream-inf", pending_stream_inf,
                           "EXT-X-STREAM-INF is not followed by a URI line")

    def check_tag(self, tag, value, number):
        if tag == "#EXTM3U":
            self.add_issue(ERROR, "header", number,
                           "#EXTM3U appears more than once")
        elif tag == "#EXT-X-VERSION":
            self.check_version(value, number)
        elif tag == "#EXT-X-I-FRAME-STREAM-INF":
            self.check_iframe_stream_inf(value, number)
        elif tag == "#EXT-X-MEDIA":
            self.check_media(value, number)
        elif tag in MEDIA_PLAYLIST_TAGS:
            self.add_issue(ERROR, "syntax", number,
                           f"{tag} is a media playlist tag and must not "
                           "appear in a master playlist")
        elif tag not in OTHER_MASTER_TAGS:
            self.add_issue(WARNING, "syntax", number,
                           f"unrecognised tag {tag}")

    def check_version(self, value, number):
        if self.version_line:
            self.add_issue(ERROR, "version", number,
                           "EXT-X-VERSION appears more than once (first on "
                           f"line {self.version_line})")
            return
        self.version_line = number
        if not value.isdigit() or int(value) < 1:
            self.add_issue(ERROR, "version", number,
                           f"EXT-X-VERSION must be a positive integer, "
                           f"found '{value}'")
            return
        self.result.version = int(value)

    def check_attributes(self, tag, value, number, spec, required, category):
        """Validate an attribute list against a spec; return the dict."""
        attributes, problems = parse_attribute_list(value)
        for problem in problems:
            self.add_issue(ERROR, category, number, f"{tag}: {problem}")
        for name in required:
            if name not in attributes:
                self.add_issue(ERROR, category, number,
                               f"{tag} is missing required attribute {name}")
        for name, raw in attributes.items():
            kind = spec.get(name)
            if kind is None:
                self.add_issue(WARNING, category, number,
                               f"{tag} has unrecognised attribute {name}")
                continue
            pattern, description = VALUE_TYPES[kind]
            if not pattern.fullmatch(raw):
                self.add_issue(ERROR, category, number,
                               f"{tag}: {name}={raw} should be {description}")
        return attributes

    def check_bandwidth(self, tag, attributes, number, category):
        bandwidth = attributes.get("BANDWIDTH", "")
        average = attributes.get("AVERAGE-BANDWIDTH", "")
        if bandwidth.isdigit() and int(bandwidth) == 0:
            self.add_issue(ERROR, category, number,
                           f"{tag}: BANDWIDTH must be greater than 0")
        if (bandwidth.isdigit() and average.isdigit()
                and int(average) > int(bandwidth)):
            self.add_issue(WARNING, category, number,
                           f"{tag}: AVERAGE-BANDWIDTH ({average}) exceeds "
                           f"peak BANDWIDTH ({bandwidth})")

    def check_resolution(self, tag, attributes, number, category):
        resolution = attributes.get("RESOLUTION", "")
        if VALUE_TYPES["resolution"][0].fullmatch(resolution):
            width, height = (int(part) for part in resolution.split("x"))
            if width == 0 or height == 0:
                self.add_issue(ERROR, category, number,
                               f"{tag}: RESOLUTION {resolution} has a zero "
                               "dimension")

    def check_stream_inf(self, value, number):
        tag = "EXT-X-STREAM-INF"
        self.result.variant_count += 1
        attributes = self.check_attributes(
            tag, value, number, STREAM_INF_ATTRIBUTES,
            required=["BANDWIDTH"], category="stream-inf")
        self.check_bandwidth(tag, attributes, number, "stream-inf")
        self.check_resolution(tag, attributes, number, "stream-inf")
        if "CODECS" not in attributes:
            self.add_issue(WARNING, "stream-inf", number,
                           f"{tag} should include a CODECS attribute")
        for group_type in ("AUDIO", "VIDEO", "SUBTITLES", "CLOSED-CAPTIONS"):
            raw = attributes.get(group_type)
            if raw and raw != "NONE":
                self.group_refs.append((group_type, unquote(raw), number))

    def check_iframe_stream_inf(self, value, number):
        tag = "EXT-X-I-FRAME-STREAM-INF"
        self.result.iframe_count += 1
        attributes = self.check_attributes(
            tag, value, number, I_FRAME_ATTRIBUTES,
            required=["BANDWIDTH", "URI"], category="i-frame")
        self.check_bandwidth(tag, attributes, number, "i-frame")
        self.check_resolution(tag, attributes, number, "i-frame")
        if "VIDEO" in attributes:
            self.group_refs.append(
                ("VIDEO", unquote(attributes["VIDEO"]), number))
        if "URI" in attributes:
            self.record_uri(unquote(attributes["URI"]), number)

    def check_media(self, value, number):
        tag = "EXT-X-MEDIA"
        self.result.media_count += 1
        attributes = self.check_attributes(
            tag, value, number, MEDIA_ATTRIBUTES,
            required=["TYPE", "GROUP-ID", "NAME"], category="media")
        media_type = attributes.get("TYPE")
        if media_type and media_type not in MEDIA_TYPES:
            self.add_issue(ERROR, "media", number,
                           f"{tag}: TYPE={media_type} must be one of "
                           f"{', '.join(sorted(MEDIA_TYPES))}")

        if media_type == "CLOSED-CAPTIONS":
            if "URI" in attributes:
                self.add_issue(ERROR, "media", number,
                               f"{tag}: CLOSED-CAPTIONS must not have a URI")
            if "INSTREAM-ID" not in attributes:
                self.add_issue(ERROR, "media", number,
                               f"{tag}: CLOSED-CAPTIONS requires INSTREAM-ID")
        elif "INSTREAM-ID" in attributes:
            self.add_issue(ERROR, "media", number,
                           f"{tag}: INSTREAM-ID is only allowed when "
                           "TYPE=CLOSED-CAPTIONS")
        if media_type == "SUBTITLES" and "URI" not in attributes:
            self.add_issue(ERROR, "media", number,
                           f"{tag}: SUBTITLES requires a URI")
        if "FORCED" in attributes and media_type != "SUBTITLES":
            self.add_issue(ERROR, "media", number,
                           f"{tag}: FORCED is only allowed when "
                           "TYPE=SUBTITLES")
        if (attributes.get("DEFAULT") == "YES"
                and attributes.get("AUTOSELECT") == "NO"):
            self.add_issue(ERROR, "media", number,
                           f"{tag}: AUTOSELECT must be YES when "
                           "DEFAULT=YES")

        if media_type in MEDIA_TYPES and "GROUP-ID" in attributes:
            key = (media_type, unquote(attributes["GROUP-ID"]))
            names = self.media_groups.setdefault(key, set())
            name = unquote(attributes.get("NAME", ""))
            if name and name in names:
                self.add_issue(ERROR, "media", number,
                               f"{tag}: NAME \"{name}\" is used twice in "
                               f"{media_type} group \"{key[1]}\"")
            names.add(name)
        if "URI" in attributes:
            self.record_uri(unquote(attributes["URI"]), number)

    def check_group_refs(self):
        referenced = set()
        for group_type, group_id, number in self.group_refs:
            referenced.add((group_type, group_id))
            if (group_type, group_id) not in self.media_groups:
                self.add_issue(ERROR, "group-refs", number,
                               f"{group_type}=\"{group_id}\" has no matching "
                               f"EXT-X-MEDIA TYPE={group_type} group")
        for group_type, group_id in self.media_groups:
            if (group_type, group_id) not in referenced:
                self.add_issue(WARNING, "group-refs", None,
                               f"{group_type} group \"{group_id}\" is never "
                               "referenced by a variant stream")

    def check_uris_exist(self):
        missing_locally = []
        for uri, number in self.result.uris.items():
            if any(severity == ERROR
                   for severity, _ in check_uri_format(uri)):
                self.result.uri_status[uri] = "skipped (bad format)"
                continue

            is_absolute = bool(urllib.parse.urlparse(uri).scheme)
            if self.base_url or is_absolute:
                if self.base_url:
                    target = urllib.parse.urljoin(self.base_url, uri)
                else:
                    target = uri
                exists, detail = check_remote_uri(target)
                self.result.uri_status[uri] = (
                    f"found ({detail})" if exists else f"missing ({detail})")
                if exists is False:
                    self.add_issue(ERROR, "uri-exists", number,
                                   f"{target} returned {detail}")
                elif exists is None:
                    self.result.uri_status[uri] = f"unknown ({detail})"
                    self.add_issue(WARNING, "uri-exists", number,
                                   f"could not check {target}: {detail}")
                continue

            relative_path = urllib.parse.unquote(
                urllib.parse.urlparse(uri).path)
            local_path = self.result.path.parent / relative_path
            if local_path.is_file():
                self.result.uri_status[uri] = "found (local file)"
            else:
                self.result.uri_status[uri] = "not found locally"
                missing_locally.append((uri, number))

        if not missing_locally:
            return
        hint = ("pass --base-url <manifest URL> to check them on the "
                "origin server")
        if len(missing_locally) == len(self.result.uris) > 1:
            self.add_issue(WARNING, "uri-exists", None,
                           f"none of the {len(missing_locally)} URIs exist "
                           f"next to the manifest; {hint}")
        else:
            for uri, number in missing_locally:
                self.add_issue(WARNING, "uri-exists", number,
                               f"'{uri}' does not exist next to the "
                               f"manifest; {hint}")


def print_report(result, base_url=None):
    """Print a human-readable validation report."""
    title = "HLS Manifest Validation Report"
    print(title)
    print("=" * len(title))
    print(f"File:      {result.path}")
    print("Type:      Master playlist")
    print(f"Version:   {result.version if result.version else '-'}")
    if base_url:
        print(f"Base URL:  {base_url}")
    print()

    print("Contents")
    print("--------")
    print(f"  Variant streams (EXT-X-STREAM-INF)          "
          f"{result.variant_count:>4}")
    print(f"  I-frame streams (EXT-X-I-FRAME-STREAM-INF)  "
          f"{result.iframe_count:>4}")
    print(f"  Renditions (EXT-X-MEDIA)                    "
          f"{result.media_count:>4}")
    print(f"  Unique URIs                                 "
          f"{len(result.uris):>4}")
    print()

    print("Checks")
    print("------")
    for category, description in CATEGORIES:
        severities = {issue.severity for issue in result.issues
                      if issue.category == category}
        if ERROR in severities:
            status = "FAIL"
        elif WARNING in severities:
            status = "WARN"
        else:
            status = "PASS"
        print(f"  [{status}] {description}")
    print()

    if result.uris:
        print("URIs")
        print("----")
        width = max(len(uri) for uri in result.uris)
        for uri in result.uris:
            status = result.uri_status.get(uri, "-")
            print(f"  {uri:<{width}}  {status}")
        print()

    print("Issues")
    print("------")
    if not result.issues:
        print("  None")
    for issue in sorted(result.issues,
                        key=lambda item: (item.severity != ERROR,
                                          item.line or 0)):
        location = f"line {issue.line}" if issue.line else "file"
        print(f"  {issue.severity:<7}  {location:<9}  {issue.message}")
    print()

    errors, warnings = result.error_count, result.warning_count
    if errors:
        verdict = "FAILED"
    elif warnings:
        verdict = "PASSED with warnings"
    else:
        verdict = "PASSED"
    print(f"Result: {verdict} ({errors} error{'s' * (errors != 1)}, "
          f"{warnings} warning{'s' * (warnings != 1)})")


def main():
    parser = argparse.ArgumentParser(
        description="Validate an HLS master playlist (.m3u8).")
    parser.add_argument(
        "manifest", nargs="?", default=str(DEFAULT_MANIFEST),
        help="path to the .m3u8 file "
             "(default: samples/master_manifest/sample_master1.m3u8)")
    parser.add_argument(
        "--base-url",
        help="URL the manifest was downloaded from; relative URIs are "
             "resolved against it and checked over HTTP")
    args = parser.parse_args()

    result = ManifestValidator(args.manifest, args.base_url).validate()
    print_report(result, args.base_url)
    return 1 if result.error_count else 0


if __name__ == "__main__":
    sys.exit(main())
