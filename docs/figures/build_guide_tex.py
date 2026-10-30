"""Build docs/AnaTele_Illustrated_Guide.tex from the Markdown chapters in docs/learn/.

Run from the repository root:  python docs/figures/build_guide_tex.py
Then compile twice with pdflatex. Only the Markdown subset used by the guide is supported.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEARN = ROOT / "docs" / "learn"
SITE = "https://avinashistoocool.github.io/AnaTele/learn/"
CHAPTERS = ["01-drones-101", "02-logs-and-mavlink", "03-time-and-sources", "04-flight-phases",
            "05-attitude-and-tilt-error", "06-motors-and-mixing", "07-vibration-ekf-compass", "08-rules-and-states",
            "09-hypotheses", "10-missions-and-geometry", "11-simulator", "12-sharing-ant"]
BOX = {"tip": ("Tip", "green!40!black"), "note": ("Note", "blue!50!black"), "info": ("Note", "blue!50!black"),
       "warning": ("Careful", "orange!80!black"), "danger": ("Important", "red!60!black"),
       "example": ("Check your understanding", "violet!70!black"), "success": ("Answer", "green!40!black")}
UNICODE = {"\u2013": "--", "\u2014": "---", "\u2019": "'", "\u2018": "`", "\u201c": "``", "\u201d": "''",
           "\u2026": "\\ldots{}", "\u00b0": "\\textdegree{}", "\u00b2": "\\textsuperscript{2}", "\u00b5": "\\textmu{}",
           "\u2192": "$\\rightarrow$", "\u2265": "$\\geq$", "\u2264": "$\\leq$", "\u2212": "$-$", "\u00b7": "$\\cdot$",
           "\u2248": "$\\approx$", "\u00d7": "$\\times$"}


def escape(text):
    out = []
    for ch in text:
        if ch in "&%#_{}$":
            out.append("\\" + ch)
        elif ch == "\\":
            out.append("\\textbackslash{}")
        elif ch == "~":
            out.append("\\textasciitilde{}")
        elif ch == "^":
            out.append("\\textasciicircum{}")
        elif ch in UNICODE:
            out.append(UNICODE[ch])
        else:
            out.append(ch)
    return "".join(out)


def inline(text):
    """Convert inline Markdown (code, math, links, bold, italic) to LaTeX."""
    tokens = []

    def stash(value):
        tokens.append(value)
        return f"\x00{len(tokens) - 1}\x00"

    text = re.sub(r"`([^`]+)`", lambda m: stash("\\texttt{" + escape(m.group(1)).replace("\\_", "\\_\\allowbreak{}") + "}"), text)
    text = re.sub(r"\$([^$]+)\$", lambda m: stash("$" + m.group(1) + "$"), text)

    def label_tex(label):
        return re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], escape(label).replace("\\textbackslash{}", ""))

    def link(m):
        label, target = m.group(1), m.group(2)
        if target.startswith("http"):
            url = target
        elif target.endswith(".pdf"):
            url = "https://avinashistoocool.github.io/AnaTele/" + target.split("/")[-1]
        else:
            page, _, anchor = target.partition("#")
            page = page.replace(".md", "/").replace("../", "")
            base = "https://avinashistoocool.github.io/AnaTele/" if target.startswith("../") else SITE
            url = base + ("" if page in ("index/", "") else page) + (("#" + anchor) if anchor else "")
        return stash("\\href{" + url.replace("#", "\\#").replace("%", "\\%") + "}{" + label_tex(label) + "}")

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, text)
    text = escape(text)
    text = re.sub(r'"([^"]+)"', r"``\1''", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\\emph{\1}", text)
    for _ in range(3):
        text = re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], text)
    return text


def table(rows):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows if not re.match(r"^\|[-\s|:]+\|$", r.strip())]
    n = len(cells[0])
    spec = "".join(["p{0.30\\linewidth}" if n == 2 and i == 0 else "X" for i in range(n)]) if n > 1 else "X"
    out = ["\\begin{center}\\small", "\\begin{tabularx}{\\linewidth}{" + spec + "}", "\\toprule"]
    for i, row in enumerate(cells):
        row = (row + [""] * n)[:n]
        out.append(" & ".join(inline(c) for c in row) + " \\\\")
        if i == 0:
            out.append("\\midrule")
    out += ["\\bottomrule", "\\end{tabularx}", "\\end{center}"]
    return out


def convert(md, chapter_slug=None):
    lines = md.splitlines()
    out, i = [], 0
    stack = []  # open admonition boxes with their indent
    in_list = None
    while i < len(lines):
        raw = lines[i]
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while stack and line and indent < stack[-1]:
            out.append("\\end{guidebox}")
            stack.pop()
        if in_list and not re.match(r"^(\d+\.|-)\s", line) and not (line and indent >= 2 and not stack):
            out.append("\\end{%s}" % in_list)
            in_list = None
        if not line:
            out.append("")
        elif line.startswith("```"):
            i += 1
            code = []
            while not lines[i].strip().startswith("```"):
                code.append(lines[i].strip())
                i += 1
            out += ["\\begin{verbatim}"] + code + ["\\end{verbatim}"]
        elif line.startswith("$$"):
            body = [line[2:]]
            while not body[-1].rstrip().endswith("$$"):
                i += 1
                body.append(lines[i].strip())
            body[-1] = body[-1].rstrip()[:-2]
            out += ["\\[", " ".join(b for b in body if b), "\\]"]
        elif line.startswith("# "):
            title = re.sub(r"^\d+\.\s*", "", line[2:])
            out.append("\\chapter{" + inline(title) + "}")
            if chapter_slug:
                out.append("\\label{ch:" + chapter_slug + "}")
        elif line.startswith("## "):
            out.append("\\section{" + inline(line[3:]) + "}")
        elif line.startswith("!["):
            m = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", line)
            path = m.group(2).replace("../images/", "images/")
            out += ["\\begin{figure}[H]\\centering", "\\includegraphics[width=\\linewidth,height=0.42\\textheight,"
                    "keepaspectratio]{" + path + "}", "\\caption{" + inline(m.group(1)) + "}", "\\end{figure}"]
        elif line.startswith('<div class="anatele-widget"'):
            name = re.search(r'data-widget="([^"]+)"', line).group(1)
            url = SITE + (chapter_slug + "/" if chapter_slug else "")
            out.append("\\begin{guidebox}{Interactive}{gray!60!black}This page has an interactive \\emph{" + name +
                       "} widget in the web version: \\url{" + url + "}\\end{guidebox}")
        elif re.match(r"^(!!!|\?\?\?)\s", line):
            m = re.match(r"^(?:!!!|\?\?\?)\s+(\w+)(?:\s+\"([^\"]*)\")?", line)
            title, colour = BOX.get(m.group(1), ("Note", "blue!50!black"))
            out.append("\\begin{guidebox}{" + inline(m.group(2) or title) + "}{" + colour + "}")
            stack.append(indent + 4)
        elif line.startswith("=== "):
            out.append("\\paragraph{" + inline(line[4:].strip('"')) + "}")
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out += table(rows)
            continue
        elif re.match(r"^(\d+\.|-)\s", line):
            kind = "enumerate" if line[0].isdigit() else "itemize"
            if in_list != kind:
                if in_list:
                    out.append("\\end{%s}" % in_list)
                out.append("\\begin{%s}" % kind)
                in_list = kind
            out.append("\\item " + inline(re.sub(r"^(\d+\.|-)\s+", "", line)))
        elif line == "---":
            out.append("\\medskip\\hrule\\medskip")
        else:
            out.append(inline(line))
        i += 1
    if in_list:
        out.append("\\end{%s}" % in_list)
    out += ["\\end{guidebox}"] * len(stack)
    return "\n".join(out)


PREAMBLE = r"""\documentclass[11pt,a4paper,openany]{book}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern,textcomp}
\usepackage[margin=2.2cm]{geometry}
\usepackage{graphicx,float,booktabs,tabularx,amsmath,amssymb,xcolor,enumitem}
\usepackage[most]{tcolorbox}
\usepackage[hidelinks]{hyperref}
\usepackage{microtype}
\graphicspath{{./}}
\definecolor{accent}{HTML}{FF5722}
\hypersetup{pdftitle={AnaTele Illustrated Guide: learn drone flight-log investigation from scratch},
  pdfauthor={Avinash Chandra Gupta},
  pdfsubject={ArduPilot flight log analysis, drone crash investigation},
  pdfkeywords={AnaTele, Avinash Chandra Gupta, Avinash Gupta, ArduPilot, MAVLink, drone, crash investigation, flight log, tutorial}}
\newtcolorbox{guidebox}[2]{enhanced,breakable,colback=#2!5!white,colframe=#2,fonttitle=\bfseries,title={#1},
  boxrule=0.6pt,arc=2pt,left=6pt,right=6pt}
\setlist{itemsep=2pt}
\setlength{\parskip}{0.5em}
\setlength{\parindent}{0pt}
\emergencystretch=2em
\begin{document}
\begin{titlepage}
\centering
\vspace*{2cm}
\includegraphics[width=0.75\linewidth]{images/guide/08_pipeline.png}\par\vspace{1.5cm}
{\Huge\bfseries AnaTele Illustrated Guide\par}\vspace{0.4cm}
{\Large Learn drone flight-log investigation from scratch\par}\vspace{1.2cm}
{\large Avinash Chandra Gupta\par}\vspace{0.3cm}
{\normalsize \url{https://avinashistoocool.github.io/AnaTele/learn/}\par}
\vfill
{\small Every chart is drawn from real simulator flights analysed by AnaTele itself.\\
Generated from the web guide by \texttt{docs/figures/build\_guide\_tex.py}.\par}
\end{titlepage}
\tableofcontents
"""


def main():
    parts = [PREAMBLE]
    intro = (LEARN / "index.md").read_text(encoding="utf-8")
    intro = intro.split("## Reading map")[0] + "## How to use this course" + intro.split("## How to use this course")[1]
    intro = re.sub(r"!!! tip \"Prefer paper\?\"\n(    .*\n)+", "", intro)
    intro = intro.replace("# Learn AnaTele from scratch", "# Introduction")
    parts.append(convert(intro))
    for slug in CHAPTERS:
        parts.append(convert((LEARN / f"{slug}.md").read_text(encoding="utf-8"), slug))
    parts.append("\\appendix")
    parts.append(convert((LEARN / "glossary.md").read_text(encoding="utf-8")))
    parts.append("\\end{document}\n")
    target = ROOT / "docs" / "AnaTele_Illustrated_Guide.tex"
    target.write_text("\n".join(parts), encoding="utf-8")
    print("wrote", target.relative_to(ROOT))


if __name__ == "__main__":
    main()
