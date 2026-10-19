# HLS Manifest Validator

`source/validator.py` checks an HLS master playlist (`.m3u8`) against the core rules of the HLS specification (RFC 8216) and prints a validation report.

It checks that:

- The file starts with the `#EXTM3U` tag, and the tag appears only once.
- `#EXT-X-VERSION` appears at most once and is a positive integer.
- `#EXT-X-STREAM-INF`, `#EXT-X-I-FRAME-STREAM-INF` and `#EXT-X-MEDIA` tags have well-formed attribute lists, required attributes are present, and every value has the right type (integer, `WIDTHxHEIGHT` resolution, quoted string, `YES`/`NO`, and so on).
- Every `#EXT-X-STREAM-INF` tag is followed by a URI line.
- `AUDIO`, `VIDEO`, `SUBTITLES` and `CLOSED-CAPTIONS` group references point to a declared `#EXT-X-MEDIA` group.
- Every URI is correctly formatted (no whitespace or backslashes, `http`/`https` only for absolute URLs) and exists, either on disk or on the origin server.
- Tags that belong only in media playlists, such as `#EXTINF`, do not appear in the master playlist.

The script uses only the Python standard library.

## Input and output

### Input

| Input | Required | Description |
|---|---|---|
| `MANIFEST` | No | Path to an HLS **master** playlist. Defaults to `samples/master_manifest/sample_master1.m3u8`. |
| `--base-url URL` | No | The URL the manifest was downloaded from. When given, relative URIs are resolved against it and checked over HTTP. Without it, URIs are checked as files next to the manifest. |

The included sample, `samples/master_manifest/sample_master1.m3u8`, is Apple's public "bipbop" HLS test stream. It was downloaded from
`https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8`.

### Output

The script prints a validation report to the console. The report has these sections:

- **Header:** the file path, playlist type, and `EXT-X-VERSION`.
- **Contents:** how many variant streams, I-frame streams, renditions and unique URIs the manifest has.
- **Checks:** a `PASS`, `WARN` or `FAIL` line for each group of checks.
- **URIs:** every referenced URI and whether it was found.
- **Issues:** each error and warning, with its line number.
- **Result:** `PASSED`, `PASSED with warnings`, or `FAILED`, with the error and warning counts.

Example report, shortened:

```
HLS Manifest Validation Report
==============================
File:      ...\samples\master_manifest\sample_master1.m3u8
Type:      Master playlist
Version:   6

Checks
------
  [PASS] File starts with #EXTM3U
  [PASS] EXT-X-STREAM-INF tags are well formed
  [PASS] Rendition group references resolve
  [PASS] URIs exist
  ...

Result: PASSED (0 errors, 0 warnings)
```

**Exit code:** `0` if there are no errors (warnings are allowed) and `1` if there are any errors. This means the script can be used in CI pipelines.

## How to run

### Requirements

- Python 3.10 or newer. No third-party packages are needed.

### Setup (optional virtual environment)

```bash
python -m venv venv-manifest-validator

# Windows
venv-manifest-validator\Scripts\activate
# macOS / Linux
source venv-manifest-validator/bin/activate
```

### Usage

Run these from the project root:

```bash
# Validate the bundled sample manifest
python source/validator.py

# Validate a different manifest
python source/validator.py path/to/master.m3u8

# Also check that every URI exists on the origin server
python source/validator.py --base-url https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8

# Show all options
python source/validator.py --help
```

Without `--base-url`, the bundled sample reports one warning. That's because only the master manifest is stored locally, not the child playlists it references. Pass `--base-url` to check those URIs on Apple's server instead.

## Project structure

```
claude-video-manifest-validator/
├── source/
│   └── validator.py              # The validator script
├── samples/
│   └── master_manifest/
│       └── sample_master1.m3u8   # Sample input (Apple bipbop test stream)
├── .claude/                      # Claude Code settings (autopep8 format hook)
├── CLAUDE.md                     # Coding rules for Claude Code
└── README.md
```
