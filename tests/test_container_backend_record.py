"""ADR 0013's invariant map and its amendments, kept checkable (#267).

ADR 0013 designs the container sandbox backend and claims, in its §5, to
mark every invariant of ADRs 0002, 0004, 0005, 0006 and 0008 as preserved,
amended or superseded, and to amend each amended one in its own record
with a dated #267 amendment. Both claims are about text, and both go stale
silently: a decision added to one of those records later, or an amendment
pointer added or dropped, would leave the map wrong with nothing to notice.
These tests read the records only. Whether a decision is rightly marked is
review's business; that the map is complete, consistent with the records'
own amendment notes and with ADR 0013's own list of its amendments, numbers
no protocol, and links where it says it does is theirs.
"""

from __future__ import annotations

import re
from collections import Counter
from functools import cache
from pathlib import Path

import autoforge

ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = ROOT / "docs" / "adr"
ADR_0013 = ADR_DIR / "0013-container-sandbox-backend-lifecycle-trust-boundaries-and-rollout.md"
INDEX = ROOT / "docs" / "README.md"

# §5's subsections of ADR 0013, and the record each one maps.
MAPPED = {"5.1": "0002", "5.2": "0004", "5.3": "0005", "5.4": "0006", "5.5": "0008"}
STATUSES = {"**preserved**", "**amended**", "**superseded**"}
# #267's amendments carry the day each was written: 2026-11-08 for all but
# ADR 0008 D10.5's, which is dated 2026-11-09.
MARKER = re.compile(r"Amended by #267 \(2026-11-0[89]\)")

DECISION = re.compile(r"^(?:- )?\*\*(D\d+\.\d+[a-z]?)\b")
KIND = re.compile(r"^#### (K\d+)\. ")
GUARANTEE = re.compile(r"^\d\. \*\*(G\d)\. ")
SECTION = re.compile(r"^## (\d+[a-z]?)\. ")
BLOCK_END = re.compile(r"^(?:(?:- )?\*\*D\d+\.\d+[a-z]?\b|#{2,4} )")
IDENT = re.compile(r"^(?:D\d+\.\d+[a-z]?|K\d+|G\d|§\d+[a-z]?)$")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
PROTOCOL = re.compile(r"\bprotocol (?:number )?(\d+)", re.IGNORECASE)
NAMED = re.compile(r"(?<![\w.§])(D\d+\.\d+[a-z]?|§\d+[a-z]?)(?!\w|\.\d)")


def _record(number: str) -> Path:
    (path,) = ADR_DIR.glob(f"{number}-*.md")
    return path


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


@cache
def _map() -> dict[str, list[tuple[list[str], str, str]]]:
    """ADR 0013 §5's rows by mapped record: (identifiers, status, note)."""
    rows: dict[str, list[tuple[list[str], str, str]]] = {}
    current = None
    for line in _lines(ADR_0013):
        heading = re.match(r"^### (5\.\d) ADR (\d{4})$", line)
        if heading:
            assert MAPPED.get(heading[1]) == heading[2], line
            current = heading[2]
            rows[current] = []
            continue
        if line.startswith("## ") and current:
            current = None
        if current and line.startswith("| ") and not line.startswith("| Invariants"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            assert len(cells) == 3, line
            idents = [ident.strip() for ident in cells[0].split(",")]
            assert all(IDENT.match(ident) for ident in idents), line
            rows[current].append((idents, cells[1], cells[2]))
    assert set(rows) == set(MAPPED.values())
    return rows


def _starts(number: str) -> dict[str, int]:
    """Where each identifier of a record begins: its first occurrence."""
    starts: dict[str, int] = {}
    for index, line in enumerate(_lines(_record(number))):
        for pattern in (DECISION, KIND, GUARANTEE):
            match = pattern.match(line)
            if match:
                starts.setdefault(match[1], index)
        match = SECTION.match(line)
        if match and "#267" not in line:
            starts.setdefault(f"§{match[1]}", index)
    return starts


def _required(number: str) -> set[str]:
    """What the map must take of a record: every decision; ADR 0004's effect
    kinds; ADR 0002's decision and its amendment sections, which it has
    instead of numbered decisions (#267's own §4f excepted)."""
    starts = _starts(number)
    if number == "0002":
        return {ident for ident in starts if re.fullmatch(r"§3|§4[a-z]", ident)}
    return {ident for ident in starts if ident.startswith(("D", "K"))}


def _block(number: str, ident: str) -> str:
    """The text an identifier owns in its record, up to the next decision or
    heading (a section: up to the next section). A guarantee of ADR 0005
    lives in D2.1, whose block is its own."""
    lines = _lines(_record(number))
    if ident.startswith("G"):
        ident = "D2.1"
    start = _starts(number)[ident]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if ident.startswith("§") and line.startswith("## "):
            end = index
            break
        if not ident.startswith("§") and BLOCK_END.match(line):
            end = index
            break
    return "\n".join(lines[start:end])


def _slug(heading: str) -> str:
    """The anchor GitHub gives a heading (as tests/test_troubleshooting_guide.py
    computes it)."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


@cache
def _anchors(path: Path) -> frozenset[str]:
    seen: Counter[str] = Counter()
    anchors = set()
    fenced = False
    for line in _lines(path):
        if FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            slug = _slug(match[2])
            anchors.add(slug if not seen[slug] else f"{slug}-{seen[slug]}")
            seen[slug] += 1
    return frozenset(anchors)


def _broken(source: Path, target: str) -> str:
    file_part, _, anchor = target.partition("#")
    destination = (source.parent / file_part).resolve() if file_part else source
    if not destination.exists():
        return f"{source.name}: {target}: no such file"
    if anchor and destination.suffix == ".md" and anchor not in _anchors(destination):
        return f"{source.name}: {target}: no such heading"
    return ""


def _amendment_text() -> list[tuple[Path, str]]:
    """Everything #267 wrote into other records: ADR 0005's §11, ADR 0002's
    §4f, and each paragraph or header entry that names #267."""
    found = []
    for number in ("0002", "0004", "0005", "0006", "0008"):
        path = _record(number)
        text = path.read_text(encoding="utf-8")
        for section in re.split(r"\n(?=## )", text):
            if section.startswith("## ") and "#267" in section.splitlines()[0]:
                found.append((path, section))
        for paragraph in text.split("\n\n"):
            if "#267" in paragraph:
                found.append((path, paragraph))
    return found


def test_adr_0013_maps_every_decision_of_the_records_it_names():
    """Each decision of ADRs 0004, 0005, 0006 and 0008, each effect kind of
    ADR 0004 and ADR 0002's decision and amendment sections appear in
    exactly one row of ADR 0013 §5, with a status; anything else a row names
    (ADR 0005's guarantees and sections) exists in its record."""
    for number, rows in _map().items():
        named = Counter(ident for idents, _, _ in rows for ident in idents)
        assert [ident for ident, count in named.items() if count > 1] == [], number
        required = _required(number)
        assert required, number
        assert sorted(required - set(named)) == [], f"ADR {number}: unmapped"
        assert sorted(set(named) - set(_starts(number))) == [], f"ADR {number}: no such"
        for idents, status, note in rows:
            assert status in STATUSES, (number, idents, status)
            if status == "**superseded**":
                assert "ADR 0009" in note, (number, idents)


def test_adr_0013_amended_decisions_carry_a_dated_amendment_and_no_others():
    """A row marked amended links into its record, to a heading that exists,
    and the decision's own text there carries #267's dated amendment; no
    decision the map leaves preserved or superseded carries one."""
    for number, rows in _map().items():
        record = _record(number)
        status_of = {ident: status for idents, status, _ in rows for ident in idents}
        for idents, status, note in rows:
            if status != "**amended**":
                continue
            targets = [t for t in LINK.findall(note) if t.partition("#")[0] == record.name]
            assert targets, (number, idents)
            assert [_broken(ADR_0013, t) for t in targets if _broken(ADR_0013, t)] == []
            for ident in idents:
                assert MARKER.search(_block(number, ident)), (number, ident)
        # A section holds its decisions' amendments, so only a section the
        # map names is held to its own status; a guarantee lives in D2.1.
        checked = [i for i in _starts(number) if i.startswith(("D", "K")) or i in status_of]
        for ident in checked:
            if MARKER.search(_block(number, ident)) and not ident.startswith("G"):
                assert status_of.get(ident) == "**amended**", (number, ident)


def _inventory() -> dict[str, set[str]]:
    """ADR 0013's own list of what it amends (its header's **Amends** entry),
    by record: each item's decisions and sections, without the section that
    holds the amendment ("through a new §11")."""
    lines = _lines(ADR_0013)
    (start,) = [i for i, line in enumerate(lines) if line.startswith("- **Amends**")]
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("- **"))
    items: dict[str, list[str]] = {}
    current = None
    for line in lines[start + 1 : end]:
        item = re.match(r"^  - \[ADR (\d{4})\]\(", line)
        if item:
            current = item[1]
            items[current] = []
        elif not line.strip():
            current = None
        if current:
            items[current].append(line)
    named: dict[str, set[str]] = {}
    for number, text in items.items():
        joined = re.sub(r"through a new §\d+[a-z]?", "", " ".join(text))
        named[number] = set(NAMED.findall(joined))
    return named


def test_adr_0013_amendment_inventory_matches_the_map():
    """The header's list of amended decisions is the one a reader checks
    first, and it drifts apart from §5 silently: per record, it names
    exactly what §5 marks amended (a guarantee of ADR 0005 is named through
    D2.1, which holds it)."""
    inventory = _inventory()
    assert set(inventory) == set(MAPPED.values())
    for number, rows in _map().items():
        amended = {
            ident
            for idents, status, _ in rows
            if status == "**amended**"
            for ident in idents
            if not ident.startswith("G")
        }
        assert amended, number
        assert sorted(inventory[number] ^ amended) == [], f"ADR {number}"


def test_adr_0013_claims_no_state_protocol_number():
    """#267 delegates numbering to #23: neither the record nor what it wrote
    into other records names a protocol past the current one."""
    current = int(autoforge.__protocol_version__)
    texts = [(ADR_0013, ADR_0013.read_text(encoding="utf-8")), *_amendment_text()]
    assert len(texts) > 5
    claimed = [
        f"{path.name}: protocol {number}"
        for path, text in texts
        for number in PROTOCOL.findall(text)
        if int(number) > current
    ]
    assert claimed == []


def test_adr_0013_and_its_amendments_link_to_what_exists():
    """Every relative link of ADR 0013, and of what it wrote into other
    records, reaches a file and a heading that exist."""
    broken = []
    for path, text in [(ADR_0013, ADR_0013.read_text(encoding="utf-8")), *_amendment_text()]:
        fenced = False
        for line in text.splitlines():
            if FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            for target in LINK.findall(line):
                if "://" not in target and not target.startswith("mailto:"):
                    broken += [p for p in [_broken(path, target)] if p]
    assert broken == []


def test_every_adr_has_a_row_in_the_documentation_index():
    """docs/README.md's "Architecture decisions" table is how a reader finds
    a record; one without a row is found only by listing the directory."""
    index = INDEX.read_text(encoding="utf-8")
    records = sorted(p.name for p in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert records and ADR_0013.name in records
    missing = [
        name
        for name in records
        if not name.endswith(".cn.md") and f"| [ADR {name[:4]}](adr/{name}) |" not in index
    ]
    assert missing == []
