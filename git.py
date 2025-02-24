import re


def parse_numstat(diff_output):
    lines = diff_output.split("\n")
    result = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 3:
            result.append(
                {
                    "additions": int(parts[0]) if parts[0] != "-" else None,
                    "deletions": int(parts[1]) if parts[1] != "-" else None,
                    "to": parts[2],
                }
            )
    return result


def parse_tree(ls_tree_output):
    lines = ls_tree_output.split("\n")
    result = []
    for line in lines:
        parts = re.split(r"\s+", line.strip(), 4)
        if len(parts) == 5:
            result.append(
                {
                    "mode": parts[0],
                    "type": parts[1],
                    "object": parts[2],
                    "size": int(parts[3]),
                    "file": parts[4],
                }
            )
    return result
