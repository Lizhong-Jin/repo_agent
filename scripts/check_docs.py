"""Check repository Markdown links and Python/JSON examples, excluding notes."""

import ast
import json
import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def strip_fences(text):
    return re.sub(r"^```[^\n]*\n.*?^```[^\n]*$", "", text, flags=re.M | re.S)


def anchors(path):
    text = strip_fences(path.read_text(encoding="utf-8"))
    result = set(re.findall(r'<a\s+(?:id|name)="([^"]+)"', text))
    counts = {}
    for title in re.findall(r"^#{1,6}\s+(.+)", text, re.M):
        title = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", title).strip().lower()
        slug = "".join(
            c for c in title if c in "-_ " or unicodedata.category(c)[0] not in "PS"
        ).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(slug + (f"-{count}" if count else ""))
    return result


def check(root=ROOT):
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
        )
        .decode()
        .split("\0")
    )
    paths = sorted({root / n for n in names if n.endswith(".md") and not n.startswith("notes/")})
    errors = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for destination in re.findall(r"\[[^]\n]*\]\(([^\s)]+)\)", strip_fences(text)):
            if re.match(r"[a-z]+://|mailto:", destination):
                continue
            name, _, fragment = unquote(destination.strip("<>")).partition("#")
            target = (path.parent / name).resolve() if name else path
            if not target.exists() or (
                fragment and target.suffix == ".md" and fragment not in anchors(target)
            ):
                errors.append(f"{path.relative_to(root)}: invalid link {destination}")
        for language, body in re.findall(r"^```([^\n]*)\n(.*?)^```\s*$", text, re.M | re.S):
            try:
                if language == "python":
                    ast.parse(body)
                elif language == "json":
                    json.loads(body)
            except (ValueError, SyntaxError) as error:
                errors.append(f"{path.relative_to(root)}: {language} example: {error}")
    return {"documents": len(paths), "errors": errors}


if __name__ == "__main__":
    result = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result["errors"]))
