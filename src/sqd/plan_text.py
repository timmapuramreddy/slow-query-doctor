"""Read text-format EXPLAIN (ANALYZE, BUFFERS) output into the FORMAT JSON shape.

Node labels follow PostgreSQL 17 src/backend/commands/explain.c. Anything not understood is
rejected with its line number rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NUMBER = r"\d+(?:\.\d+)?"
_COST = rf"\(cost=(?P<sc>{_NUMBER})\.\.(?P<tc>{_NUMBER}) rows=(?P<pr>{_NUMBER}) width=(?P<pw>\d+)\)"
_ACTUAL = (
    rf"\((?:actual (?:time=(?P<as>{_NUMBER})\.\.(?P<at>{_NUMBER}) )?rows=(?P<ar>{_NUMBER}) "
    rf"loops=(?P<al>{_NUMBER})|(?P<never>never executed))\)"
)
# Label, then "(cost=...)" unless COSTS OFF, then "(actual ...)" with ANALYZE.
_NODE_LINE = re.compile(rf"(?P<label>\S.*?)(?:\s+{_COST})?(?:\s+{_ACTUAL})?")
_PROPERTY = re.compile(r"(?P<key>[A-Z][A-Za-z0-9 /\-]*):(?:\s+(?P<value>.*))?")
_SUBPLAN = re.compile(r"(?P<kind>SubPlan|InitPlan) \d+(?: \(returns .*\))?|CTE .+")
_TRIGGER = re.compile(r"Trigger .+: (?:time=\S+ )?calls=\d+")
_WORKER = re.compile(r"Worker \d+")
_MS = re.compile(rf"({_NUMBER}) ms")
_SORT_METHOD = re.compile(r"(?P<method>.+?)  (?P<type>Memory|Disk): (?P<used>\d+)kB")
# One name as quote_identifier prints it: plain lowercase, or quoted with "" for a quote.
_ID = r'"(?:[^"]|"")+"|[a-z_][a-z0-9_]*'
_TARGET = re.compile(rf"(?:(?P<schema>{_ID})\.)?(?P<name>{_ID})(?: (?P<alias>{_ID}))?")
_INDEX_SCAN = re.compile(
    rf"(?P<type>Index Scan|Index Only Scan)(?P<backward> Backward)? using (?P<index>{_ID}) "
    r"on (?P<target>.+)"
)
_BITMAP_INDEX_SCAN = re.compile(rf"Bitmap Index Scan on (?P<index>{_ID})")
_JOIN = re.compile(
    r"(?P<kind>Nested Loop|Hash|Merge)"
    r"(?: (?P<type>Right Semi|Right Anti|Left|Right|Full|Semi|Anti))?(?P<join> Join)?"
)
_AGGREGATE = re.compile(
    r"(?:(?P<mode>Partial|Finalize) )?"
    r"(?P<name>Aggregate|GroupAggregate|HashAggregate|MixedAggregate)"
)
_SETOP = re.compile(
    r"(?P<name>SetOp|HashSetOp) (?P<command>Intersect All|Intersect|Except All|Except)"
)
_MODIFY = re.compile(r"(?P<op>Insert|Update|Delete|Merge) on (?P<target>.+)")
_FOREIGN = re.compile(r"Foreign (?P<op>Scan|Insert|Update|Delete)(?: on (?P<target>.+))?")
_CUSTOM = re.compile(r"Custom Scan \((?P<provider>[^)]*)\)(?: on (?P<target>.+))?")
# ModifyTable lists each table it writes as "Update on t t_1" under its own label.
_MODIFY_TARGET = re.compile(r"(?:Foreign )?(?:Insert|Update|Delete|Merge) on .+")

_SIMPLE_NODES = frozenset(
    {
        "Result", "ProjectSet", "Append", "Merge Append", "Recursive Union", "BitmapAnd",
        "BitmapOr", "Gather", "Gather Merge", "Materialize", "Memoize", "Sort",
        "Incremental Sort", "Group", "WindowAgg", "Unique", "LockRows", "Limit", "Hash",
    }
)  # fmt: skip
# Scan -> the JSON key its "on <name>" goes in. None: only the alias is printed.
_SCANS: dict[str, str | None] = {
    "Seq Scan": "Relation Name",
    "Sample Scan": "Relation Name",
    "Bitmap Heap Scan": "Relation Name",
    "Tid Scan": "Relation Name",
    "Tid Range Scan": "Relation Name",
    "Function Scan": "Function Name",
    "Table Function Scan": "Table Function Name",
    "CTE Scan": "CTE Name",
    "WorkTable Scan": "CTE Name",
    "Named Tuplestore Scan": "Tuplestore Name",
    "Subquery Scan": None,
    "Values Scan": None,
}
_STRATEGIES = {
    "Aggregate": "Plain",
    "GroupAggregate": "Sorted",
    "HashAggregate": "Hashed",
    "MixedAggregate": "Mixed",
    "SetOp": "Sorted",
    "HashSetOp": "Hashed",
}
_MEMBER_PARENTS = frozenset({"Append", "Merge Append", "BitmapAnd", "BitmapOr"})

_TEXT_PROPERTIES = frozenset(
    {"Filter", "Index Cond", "Recheck Cond", "Hash Cond", "Merge Cond", "Join Filter",
     "One-Time Filter"}
)  # fmt: skip
_NUMBER_PROPERTIES = frozenset(
    {"Rows Removed by Filter", "Rows Removed by Index Recheck", "Rows Removed by Join Filter",
     "Workers Planned", "Workers Launched", "Heap Fetches"}
)  # fmt: skip
_LIST_PROPERTIES = frozenset({"Sort Key", "Group Key", "Presorted Key"})
_READ_PROPERTIES = _TEXT_PROPERTIES | _NUMBER_PROPERTIES | _LIST_PROPERTIES
# Lines printed after the tree, at the first column.
_SUMMARY = frozenset(
    {"Planning", "Planning Time", "Execution Time", "Serialization", "JIT", "Settings",
     "Query Identifier", "Memory"}
)  # fmt: skip


@dataclass
class _Frame:
    """A place child lines can hang from: a node's body, or a SubPlan/InitPlan header."""

    col: int  # column of the "->" of a child, and of the node's property lines
    node: dict[str, Any]
    subplan: tuple[str, str] | None = None  # (Parent Relationship, Subplan Name)
    repeated: set[str] = field(default_factory=set)
    line: int = 0  # line number of a SubPlan header
    has_child: bool = False  # a SubPlan header has its one node


def parse_text_explain(text: str) -> dict[str, Any]:
    """{"Plan": ..., "Planning Time": ..., "Execution Time": ...} like FORMAT JSON gives.

    Accepts psql -XqAt output and psql's default aligned output (header, dashes, footer).
    Raises ValueError naming the line it could not read.
    """
    lines = _plan_lines(text)
    result: dict[str, Any] = {}
    frames: list[_Frame] = []
    # Lines indented deeper than this column belong to a block sqd does not read.
    skip_under: tuple[int, str] | None = None  # (column, the block's first line)
    last_sort_key: tuple[int, _Frame] | None = None  # the line just before was a Sort Key
    open_header: _Frame | None = None  # a SubPlan/InitPlan/CTE header still waiting for its node
    for no, line in lines:
        indent = len(line) - len(line.lstrip(" "))
        body = line[indent:]
        if _ends_inside_quotes(body):
            raise ValueError(
                f"line {no}: a quoted value continues on the next line; text EXPLAIN cannot "
                "be read reliably here, save the plan as FORMAT JSON"
            )
        if skip_under is not None:
            if indent > skip_under[0]:
                _check_skipped(body, no, skip_under[1])
                continue
            skip_under = None
        if not frames:
            if indent or body.startswith("->"):
                raise ValueError(
                    f"line {no} should be the top plan node, starting at the first column"
                )
            result["Plan"] = _node(body, no)
            frames.append(_Frame(col=2, node=result["Plan"]))
            continue
        if open_header and not (body.startswith("->") and indent == open_header.col):
            _missing_header_child(open_header)
        open_header = None
        if indent == 0:
            _summary_line(body, no, result)
            skip_under = (0, body)  # e.g. Buffers under "Planning:" or "Serialization: ..."
            continue
        sort_key, last_sort_key = last_sort_key, None
        if body.startswith("->"):
            label = body.removeprefix("->").lstrip(" ")
            frame = _frame_at(frames, indent, no)
            if frame.subplan and frame.has_child:
                raise ValueError(
                    f'line {no} is a second plan node under "{frame.subplan[1]}" '
                    f"(line {frame.line}), which has one"
                )
            frame.has_child = True
            child = _node(label, no)
            _attach(frame, child)
            frames.append(_Frame(col=indent + 6, node=child))
            continue
        header = _SUBPLAN.fullmatch(body)
        if header:
            frame = _frame_at(frames, indent, no)
            relationship = "SubPlan" if header["kind"] == "SubPlan" else "InitPlan"
            # PostgreSQL prints exactly one node line right under a header. CTE names are
            # printed unquoted, so a name with a newline could otherwise invent lines.
            open_header = _Frame(
                col=indent + 2, node=frame.node, subplan=(relationship, body), line=no
            )
            frames.append(open_header)
            continue
        prop = _PROPERTY.fullmatch(body)
        if prop and prop["key"] == "Group Key" and sort_key and indent > sort_key[0]:
            # Grouping sets print a Sort Key with the Group Keys it serves indented under it.
            _grouping_sets(sort_key[1])
            last_sort_key = sort_key
            continue
        if prop or _MODIFY_TARGET.fullmatch(body):
            frame = _frame_at(frames, indent, no)
            if prop and prop["key"] == "Sort Key" and frame.node["Node Type"] == "Aggregate":
                last_sort_key = (indent, frame)
            if frame.subplan:
                raise ValueError(f"line {no} is indented under a SubPlan header, not a node")
            if prop and not prop["value"] and prop["key"] in _READ_PROPERTIES:
                raise ValueError(f'line {no}: "{prop["key"]}" has no value on its line')
            if prop is None or not prop["value"] or _WORKER.fullmatch(prop["key"]):
                skip_under = (indent, body)  # a block such as "Worker 0:" with lines under it
            else:
                _set_property(frame, prop["key"], prop["value"], no)
            continue
        raise ValueError(f"line {no} is not a plan node, property or summary line")
    if open_header:
        _missing_header_child(open_header)
    _finish(result["Plan"])
    return result


def _check_skipped(body: str, no: int, block: str) -> None:
    """Reject a skipped line that real EXPLAIN output never puts inside a block."""
    prop = _PROPERTY.fullmatch(body)
    if body.startswith("->") or _SUBPLAN.fullmatch(body):
        raise ValueError(
            f'line {no} is a plan node indented under "{block}", which is not a plan node'
        )
    if prop and prop["key"] in _READ_PROPERTIES:
        raise ValueError(
            f'line {no}: "{prop["key"]}" is indented under "{block}", which is not a plan node'
        )


def _missing_header_child(header: _Frame) -> None:
    assert header.subplan is not None
    raise ValueError(
        f'line {header.line}: "{header.subplan[1]}" is not followed by its plan node '
        "on the next line"
    )


def _ends_inside_quotes(line: str) -> bool:
    """True if a '...' literal or "..." name is still open at the end of the line.

    A quote of the other kind inside a quoted run is plain text; a doubled delimiter ('' or
    "") closes and reopens the run, so escapes need no special case.
    """
    quote = ""
    for char in line:
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
    return bool(quote)


def _plan_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) for each non-blank line, with psql's aligned table removed."""
    rows = [(no, line.rstrip()) for no, line in enumerate(text.splitlines(), 1)]
    rows = [(no, line) for no, line in rows if line]
    if not rows:
        raise ValueError("the input is empty")
    if rows[0][1].strip() == "QUERY PLAN":
        rows = _table_rows(rows)
    for no, line in rows:
        if line.lstrip().startswith('"'):
            raise ValueError(
                f"line {no} is wrapped in double quotes (CSV output); save the plan with "
                "psql -XqAt instead"
            )
    return rows


def _table_rows(rows: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """The plan lines inside psql's aligned table: header, dashes, " row"s, "(N rows)"."""
    if len(rows) < 2 or not re.fullmatch(r"-+", rows[1][1]):
        raise ValueError(f"line {rows[0][0]} is a QUERY PLAN header without the dashes under it")
    rows = rows[2:]
    if rows and re.fullmatch(r"\(\d+ rows?\)", rows[-1][1]):
        rows = rows[:-1]
    if not rows:
        raise ValueError("the input has a QUERY PLAN header but no plan")
    if rows[0][1].lstrip().startswith(("[", "{")):
        raise ValueError(
            f"line {rows[0][0]} starts FORMAT JSON output that psql printed as a table; "
            "save it with psql -XqAt so the JSON is not wrapped"
        )
    for no, line in rows:
        if not line.startswith(" "):
            raise ValueError(f"line {no} does not start with the space psql puts before each row")
    return [(no, line[1:]) for no, line in rows]


def _frame_at(frames: list[_Frame], indent: int, no: int) -> _Frame:
    """The node or header a line at this indent belongs to; closes deeper ones."""
    while frames and frames[-1].col > indent:
        frames.pop()
    if not frames or frames[-1].col != indent:
        raise ValueError(f"line {no} is indented to a column that matches no plan node above it")
    return frames[-1]


def _attach(frame: _Frame, child: dict[str, Any]) -> None:
    """Add a child node, with the Parent Relationship JSON EXPLAIN gives it."""
    parent = frame.node
    plans = parent.setdefault("Plans", [])
    if frame.subplan:
        child["Parent Relationship"], child["Subplan Name"] = frame.subplan
    elif parent["Node Type"] in _MEMBER_PARENTS:
        child["Parent Relationship"] = "Member"
    elif parent["Node Type"] == "ModifyTable":
        pass  # Member before PostgreSQL 14, Outer since; text does not show the version
    elif parent["Node Type"] == "Custom Scan":
        pass  # "child" or "children" once all are known: see _finish
    elif parent["Node Type"] == "Subquery Scan":
        child["Parent Relationship"] = "Subquery"
    else:
        regular = [p for p in plans if "Subplan Name" not in p]
        child["Parent Relationship"] = "Outer" if not regular else "Inner"
    plans.append(child)


def _node(line: str, no: int) -> dict[str, Any]:
    """One plan node from its line: label, cost block and actual block."""
    match = _NODE_LINE.fullmatch(line)
    node = _read_label(match["label"]) if match else None
    if node is None:
        raise ValueError(f'line {no}: "{line}" is not a plan node sqd can read')
    if match["sc"] is not None:
        node["Startup Cost"] = float(match["sc"])
        node["Total Cost"] = float(match["tc"])
        node["Plan Rows"] = _number(match["pr"])
        node["Plan Width"] = int(match["pw"])
    if match["never"]:
        node["Actual Rows"] = 0
        node["Actual Loops"] = 0
    elif match["ar"] is not None:
        if match["as"] is not None:
            node["Actual Startup Time"] = float(match["as"])
            node["Actual Total Time"] = float(match["at"])
        node["Actual Rows"] = _number(match["ar"])
        node["Actual Loops"] = _number(match["al"])
    return node


def _read_label(label: str) -> dict[str, Any] | None:
    """Node Type and the fields its label carries, e.g. "Hash Left Join", or None."""
    parallel = label.startswith("Parallel ")
    label = label.removeprefix("Parallel ")
    asynchronous = label.startswith("Async ")
    label = label.removeprefix("Async ")
    fields = _label_fields(label)
    if fields is None:
        return None
    return {**fields, "Parallel Aware": parallel, "Async Capable": asynchronous}


def _label_fields(label: str) -> dict[str, Any] | None:
    if label in _SIMPLE_NODES:
        return {"Node Type": label}
    if m := _JOIN.fullmatch(label):
        if m["kind"] == "Nested Loop" and bool(m["type"]) == bool(m["join"]):
            return {"Node Type": "Nested Loop", "Join Type": m["type"] or "Inner"}
        if m["kind"] != "Nested Loop" and m["join"]:
            return {"Node Type": f"{m['kind']} Join", "Join Type": m["type"] or "Inner"}
        return None
    if m := _AGGREGATE.fullmatch(label):
        return {
            "Node Type": "Aggregate",
            "Strategy": _STRATEGIES[m["name"]],
            "Partial Mode": m["mode"] or "Simple",
        }
    if m := _SETOP.fullmatch(label):
        return {"Node Type": "SetOp", "Strategy": _STRATEGIES[m["name"]], "Command": m["command"]}
    if m := _INDEX_SCAN.fullmatch(label):
        target = _target(m["target"], "Relation Name")
        direction = "Backward" if m["backward"] else "Forward"
        node = {"Node Type": m["type"], "Scan Direction": direction}
        return node | {"Index Name": _unquote(m["index"])} | target if target else None
    if m := _BITMAP_INDEX_SCAN.fullmatch(label):
        return {"Node Type": "Bitmap Index Scan", "Index Name": _unquote(m["index"])}
    for scan, key in _SCANS.items():
        if label.startswith(f"{scan} on "):
            target = _target(label.removeprefix(f"{scan} on "), key)
            return {"Node Type": scan} | target if target else None
    if m := _FOREIGN.fullmatch(label):
        operation = "Select" if m["op"] == "Scan" else m["op"]
        return _with_target({"Node Type": "Foreign Scan", "Operation": operation}, m["target"])
    if m := _CUSTOM.fullmatch(label):
        node = {"Node Type": "Custom Scan", "Custom Plan Provider": m["provider"]}
        return _with_target(node, m["target"])
    if m := _MODIFY.fullmatch(label):
        target = _target(m["target"], "Relation Name")
        return {"Node Type": "ModifyTable", "Operation": m["op"]} | target if target else None
    return None


def _with_target(node: dict[str, Any], text: str | None) -> dict[str, Any] | None:
    """Node plus its optional "on <relation>" (a foreign or custom join has none)."""
    if text is None:
        return node
    target = _target(text, "Relation Name")
    return node | target if target else None


def _target(text: str, key: str | None) -> dict[str, Any] | None:
    """Fields for "[schema.]name [alias]". The alias is printed only when it differs."""
    m = _TARGET.fullmatch(text)
    if not m:
        return None
    name = _unquote(m["name"])
    if key is None:  # Subquery Scan and Values Scan print only the alias
        return None if m["schema"] or m["alias"] else {"Alias": name}
    fields: dict[str, Any] = {key: name}
    if m["schema"]:
        fields["Schema"] = _unquote(m["schema"])
    fields["Alias"] = _unquote(m["alias"]) if m["alias"] else name
    return fields


def _unquote(name: str) -> str:
    return name[1:-1].replace('""', '"') if name.startswith('"') else name


def _set_property(frame: _Frame, key: str, value: str, no: int) -> None:
    """Copy one "Key: value" line into the node. Keys sqd does not read are ignored."""
    node = frame.node
    if key in _TEXT_PROPERTIES:
        node[key] = value
    elif key in _NUMBER_PROPERTIES:
        if not re.fullmatch(_NUMBER, value):
            raise ValueError(f'line {no}: "{key}" should be a number')
        node[key] = _number(value)
    elif key == "Hash Key":
        _grouping_sets(frame)
    elif key in _LIST_PROPERTIES:
        if key == "Group Key" and (key in node or key in frame.repeated):
            _grouping_sets(frame)
        elif key not in frame.repeated:
            node[key] = split_top_level(value)
    elif key == "Sort Method":
        m = _SORT_METHOD.fullmatch(value)
        if m:
            node["Sort Method"] = m["method"]
            node["Sort Space Used"] = int(m["used"])
            node["Sort Space Type"] = m["type"]


def _grouping_sets(frame: _Frame) -> None:
    """Grouping sets print several Group Key, Hash Key and Sort Key lines. JSON lists them
    under "Grouping Sets" instead, so the node gets no Group Key or Sort Key."""
    frame.repeated |= {"Group Key", "Sort Key"}
    frame.node.pop("Group Key", None)
    frame.node.pop("Sort Key", None)


def _summary_line(line: str, no: int, result: dict[str, Any]) -> None:
    """Read a line after the tree, such as Execution Time. Rejects anything else."""
    if _TRIGGER.fullmatch(line):
        return
    prop = _PROPERTY.fullmatch(line)
    if prop and prop["key"] in _SUMMARY:
        key, value = prop["key"], prop["value"]
        if key in ("Planning Time", "Execution Time"):
            m = _MS.fullmatch(value or "")
            if not m:
                raise ValueError(f'line {no}: "{key}" should be a time in ms')
            result[key] = float(m[1])
        return
    node = _NODE_LINE.fullmatch(line)
    if node and (node["sc"] or node["ar"] or node["never"] or _read_label(node["label"])):
        raise ValueError(f"line {no} starts a second plan; give one plan per file")
    raise ValueError(f"line {no} is not a plan summary line such as Execution Time")


def _finish(root: dict[str, Any]) -> None:
    """Fields JSON gives that need the whole tree: Custom Scan child names, and zero times
    on never-executed nodes when the plan has times at all."""
    nodes = [root]
    for node in nodes:
        nodes.extend(node.get("Plans", []))
    for node in nodes:
        if node["Node Type"] == "Custom Scan":
            children = [p for p in node.get("Plans", []) if "Subplan Name" not in p]
            for child in children:
                child["Parent Relationship"] = "child" if len(children) == 1 else "children"
    if any("Actual Total Time" in n for n in nodes):
        for node in nodes:
            if node.get("Actual Loops") == 0 and "Actual Total Time" not in node:
                node["Actual Startup Time"] = 0.0
                node["Actual Total Time"] = 0.0


def split_top_level(text: str) -> list[str]:
    """Split a key list on commas outside parentheses, brackets and quotes.

    Example: "lower(name), (a + b) DESC" -> ["lower(name)", "(a + b) DESC"].
    """
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    for pos, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:pos].strip())
            start = pos + 1
    parts.append(text[start:].strip())
    return parts


def _number(text: str) -> int | float:
    """A count as json reads it: 579 -> int, 3.00 -> float."""
    return int(text) if text.isdigit() else float(text)
