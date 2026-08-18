#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build the MEV+SPR status brief as Anthropic-nvmeof-poc-plan_brief.docx.

Three sections, in the order the reader asked for them. Section 1 is the update:
the two flows from the previous doc are now replicated, here is what they measure
on MEV-100, here is the rig, and the next step is MMG-400 -- so the charts and
both setup figures live there. Section 2 is the ask that follows: how we want to
run the PoC so it stays small for us. Section 3 is what is still open.

This is a status update, not a solution proposal -- the reader has their own KV
cache -- so Section 2 says plainly what we are and are not building.

Figures come from ``brief_charts.py`` and ``diagrams/brief/``, all rendered at
6.5 in so Word inserts them at 100% and never resamples. Captions live in the
document rather than inside the images.

Usage:
    python build_poc_plan_brief.py [--outdir .]
"""

from __future__ import annotations

# Standard
import argparse
from pathlib import Path

# Third Party
from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

REPO_ROOT = Path(__file__).resolve().parents[2]
FIGURES = REPO_ROOT / "docs/design/v1/platform/ipu-poc/diagrams/brief"
OUTPUT_NAME = "Anthropic-nvmeof-poc-plan_brief.docx"

# Text column width for letter with 1-inch margins. Every figure is authored at
# this width, so it is inserted at exactly this width and never scaled.
COLUMN_IN = 6.5

BLUE = "2E74B5"
DARK_BLUE = "1F4D78"
INK = "0B2545"
LIGHT_GRAY = "F2F4F7"
CALLOUT = "F4F6F9"
MUTED = "666666"


def set_font(
    run,
    size: float,
    bold: bool = False,
    color: str = "000000",
    font_name: str = "Calibri",
) -> None:
    """Apply the document font to one run.

    Args:
        run: Run to style.
        size: Font size in points.
        bold: Whether the run is bold.
        color: Hex RGB string without a leading hash.
    """
    run.font.name = font_name
    run._element.rPr.rFonts.set(qn("w:ascii"), font_name)
    run._element.rPr.rFonts.set(qn("w:hAnsi"), font_name)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)


def set_cell_width(cell, inches: float) -> None:
    """Set one cell's width explicitly in DXA.

    Args:
        cell: Table cell to size.
        inches: Target width in inches.
    """
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.find(qn("w:tcW"))
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(int(inches * 1440)))
    tc_w.set(qn("w:type"), "dxa")


def set_cell_margins(cell, top: int = 60, start: int = 110,
                     bottom: int = 60, end: int = 110) -> None:
    """Set table cell padding in DXA.

    Args:
        cell: Table cell to pad.
        top: Top padding.
        start: Leading-edge padding.
        bottom: Bottom padding.
        end: Trailing-edge padding.
    """
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for side, value in (("top", top), ("start", start),
                        ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{side}"))
        if node is None:
            node = OxmlElement(f"w:{side}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_cell_shading(cell, fill: str) -> None:
    """Apply a table cell fill.

    Args:
        cell: Table cell to shade.
        fill: Hex RGB string without a leading hash.
    """
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def set_table_geometry(table, widths: list[float]) -> None:
    """Pin a table to the 6.5-inch text column with a fixed grid.

    Args:
        table: Table to size.
        widths: Per-column widths in inches.
    """
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.first_child_found_in("w:tblW")
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(int(COLUMN_IN * 1440)))
    tbl_w.set(qn("w:type"), "dxa")

    grid = table._tbl.tblGrid
    for column, width in zip(grid.findall(qn("w:gridCol")), widths, strict=False):
        column.set(qn("w:w"), str(int(width * 1440)))


def set_repeat_table_header(row) -> None:
    """Mark a table row as a repeating header row.

    Args:
        row: Row to mark.
    """
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)


def add_field(paragraph, field_code: str) -> None:
    """Insert a Word field so page numbers stay live.

    Args:
        paragraph: Paragraph to append the field to.
        field_code: Word field code, e.g. ``PAGE``.
    """
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = field_code
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend((begin, instruction, separate, text, end))


def add_body(doc: Document, text: str, keep_with_next: bool = False,
             page_break_before: bool = False) -> None:
    """Add a body paragraph.

    Args:
        doc: Document to append to.
        text: Paragraph text.
        keep_with_next: Whether Word must not break after this paragraph.
        page_break_before: Whether the paragraph starts a new page.
    """
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(5)
    paragraph.paragraph_format.line_spacing = 1.08
    paragraph.paragraph_format.keep_with_next = keep_with_next
    paragraph.paragraph_format.page_break_before = page_break_before
    set_font(paragraph.add_run(text), 10.5)


def add_bullet(doc: Document, text: str, lead: str = "",
               keep_with_next: bool = False) -> None:
    """Add a Word bullet, optionally with a bold lead-in.

    Args:
        doc: Document to append to.
        text: Bullet text following the lead-in.
        lead: Optional bold lead-in, rendered before ``text``.
        keep_with_next: Whether Word must not break after this bullet.
    """
    paragraph = doc.add_paragraph(style="List Bullet")
    paragraph.paragraph_format.space_after = Pt(3)
    paragraph.paragraph_format.line_spacing = 1.08
    paragraph.paragraph_format.keep_with_next = keep_with_next
    if lead:
        set_font(paragraph.add_run(f"{lead} "), 10.5, bold=True)
    set_font(paragraph.add_run(text), 10.5)


def add_heading(doc: Document, text: str, level: int,
                page_break_before: bool = False) -> None:
    """Add a heading that stays with the following paragraph.

    Args:
        doc: Document to append to.
        text: Heading text.
        level: Heading level.
        page_break_before: Whether the heading starts a new page.
    """
    paragraph = doc.add_heading(text, level=level)
    paragraph.paragraph_format.keep_with_next = True
    paragraph.paragraph_format.page_break_before = page_break_before


def add_figure(doc: Document, name: str, caption: str) -> None:
    """Insert one figure at authored size with a caption beneath it.

    The width is not passed to ``add_picture``: each figure is authored at the
    6.5-inch text column and 300 dpi, so letting Word use the intrinsic size
    keeps in-figure point sizes equal to on-page point sizes.

    Args:
        doc: Document to append to.
        name: Figure basename inside the brief figures directory.
        caption: Caption text, including any scope caveat.

    Raises:
        FileNotFoundError: If the figure has not been rendered.
    """
    path = FIGURES / f"{name}.png"
    if not path.exists():
        raise FileNotFoundError(
            f"missing figure {path}; run brief_charts.py and render the setup SVG first"
        )
    holder = doc.add_paragraph()
    holder.alignment = WD_ALIGN_PARAGRAPH.CENTER
    holder.paragraph_format.space_before = Pt(2)
    holder.paragraph_format.space_after = Pt(2)
    holder.paragraph_format.keep_with_next = True
    holder.add_run().add_picture(str(path), width=Inches(COLUMN_IN))

    add_caption(doc, caption)


def add_caption(doc: Document, text: str) -> None:
    """Add one caption line beneath a figure or table.

    Args:
        doc: Document to append to.
        text: Caption text, including any scope caveat.
    """
    note = doc.add_paragraph()
    note.paragraph_format.space_after = Pt(8)
    note.paragraph_format.line_spacing = 1.0
    run = note.add_run(text)
    set_font(run, 8.5, color=MUTED)
    run.font.italic = True


def style_cell(
    cell,
    text: str,
    width: float,
    header: bool = False,
    alternate: bool = False,
    size: float = 9.0,
    font_name: str = "Calibri",
) -> None:
    """Write one consistently styled table cell.

    Args:
        cell: Cell to write.
        text: Cell text.
        width: Cell width in inches.
        header: Whether this is a header cell.
        alternate: Whether to apply the alternating row fill.
        size: Font size in points.
    """
    set_cell_width(cell, width)
    set_cell_margins(cell)
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
    if header:
        set_cell_shading(cell, LIGHT_GRAY)
    elif alternate:
        set_cell_shading(cell, "FAFBFC")
    paragraph = cell.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.0
    paragraph.clear()
    set_font(
        paragraph.add_run(text),
        size,
        bold=header,
        color=INK if header else "000000",
        font_name=font_name,
    )


def add_table(
    doc: Document,
    headers: list[str],
    rows: list[list[str]],
    widths: list[float],
    body_font_name: str = "Calibri",
    body_font_size: float = 9.0,
) -> None:
    """Add a fixed-width table with a repeating header row.

    Args:
        doc: Document to append to.
        headers: Header labels.
        rows: Row values.
        widths: Per-column widths in inches.
    """
    table = doc.add_table(rows=1, cols=len(headers))
    set_table_geometry(table, widths)
    for index, header in enumerate(headers):
        style_cell(table.rows[0].cells[index], header, widths[index], header=True)
    set_repeat_table_header(table.rows[0])
    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        for index, value in enumerate(values):
            style_cell(
                cells[index],
                value,
                widths[index],
                alternate=row_index % 2 == 1,
                size=body_font_size,
                font_name=body_font_name,
            )
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(2)


def add_scope_note(doc: Document, title: str, text: str) -> None:
    """Add one shaded scope note.

    Args:
        doc: Document to append to.
        title: Bold lead-in label.
        text: Note body.
    """
    table = doc.add_table(rows=1, cols=1)
    set_table_geometry(table, [COLUMN_IN])
    cell = table.cell(0, 0)
    set_cell_width(cell, COLUMN_IN)
    set_cell_margins(cell, top=90, start=130, bottom=90, end=130)
    set_cell_shading(cell, CALLOUT)
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.08
    set_font(paragraph.add_run(f"{title}. "), 10.0, bold=True, color=DARK_BLUE)
    set_font(paragraph.add_run(text), 10.0)
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(2)


def configure(doc: Document) -> None:
    """Apply page setup, styles, header, and footer.

    Args:
        doc: Document to configure.
    """
    section = doc.sections[0]
    section.top_margin = Inches(0.8)
    section.bottom_margin = Inches(0.8)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.4)
    section.footer_distance = Inches(0.4)

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal._element.rPr.rFonts.set(qn("w:ascii"), "Calibri")
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), "Calibri")
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(5)
    normal.paragraph_format.line_spacing = 1.08

    headings = {1: (14, BLUE, 10, 5), 2: (11.5, BLUE, 8, 4)}
    for level, (size, color, before, after) in headings.items():
        style = doc.styles[f"Heading {level}"]
        style.font.name = "Calibri"
        style._element.rPr.rFonts.set(qn("w:ascii"), "Calibri")
        style._element.rPr.rFonts.set(qn("w:hAnsi"), "Calibri")
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)

    for style_name in ("List Bullet", "List Number"):
        style = doc.styles[style_name]
        style.font.name = "Calibri"
        style._element.rPr.rFonts.set(qn("w:ascii"), "Calibri")
        style._element.rPr.rFonts.set(qn("w:hAnsi"), "Calibri")
        style.font.size = Pt(10.5)

    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.LEFT
    header.paragraph_format.space_after = Pt(0)
    set_font(header.add_run("Inference KV Cache Offload over Falcon"), 8.5, color=MUTED)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.paragraph_format.space_before = Pt(0)
    set_font(footer.add_run("Page "), 8.5, color=MUTED)
    add_field(footer, "PAGE")


def build() -> Document:
    """Build the brief.

    Returns:
        The finished document.
    """
    doc = Document()
    configure(doc)

    title = doc.add_paragraph()
    title.paragraph_format.space_after = Pt(1)
    set_font(
        title.add_run("KV Cache Offload over Falcon: 100 GbE Status and PoC Scope"),
        15, bold=True, color=DARK_BLUE)
    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(8)
    set_font(subtitle.add_run(
        "Retrieve and store flows replicated on mkp1/mkp2 (Xeon SPR 6430 + "
        "MEV-100 IPU, 1x 100 GbE Falcon). Next step is MMG-400."), 10.5, color=MUTED)

    # --- 1. Results -------------------------------------------------------
    add_heading(doc, "1. Both flows are replicated; retrieve is at the wire", 1)
    add_body(
        doc,
        "The retrieve and store flows from the earlier document now run end to end on "
        "the 100 GbE Falcon link between mkp1 and mkp2, under LMCache at 256 KiB model "
        "page geometry -- the block size MMG-400 phase 1 is written against. Retrieve "
        "reaches 95.6 Gb/s, 99.7% of the fio NVMe-oF read ceiling. Raw namespaces and "
        "XFS land within 0.1 Gb/s of each other, so the link is the limiter, not our "
        "software; only local media, which never crosses the wire, is faster."
    )
    add_figure(
        doc, "brief-retrieve-ladder",
        "Figure 1. 256 KiB sequential read, QD32, three repetitions per cell, identical "
        "fio job shape across surfaces; LMCache bar is Mixtral 8x22B page geometry at "
        "32 workers. Read-only; see Figure 2 for the mixed case."
    )
    add_body(
        doc,
        "Table 1 is what makes 95.9 Gb/s a ceiling rather than one good cell: it does "
        "not move with I/O size. Three large-block sizes land within 0.2 Gb/s of each "
        "other remotely and at 107.8 locally. A media limit would vary with block "
        "size; a link limit does not.",
        keep_with_next=True,
    )
    # The ceiling table varies block size and holds the surface set, which is the
    # one axis Figure 1 does not show -- Figure 1 is a single block size across
    # surfaces, so this is the orthogonal cut, not a restatement of it. Only
    # large-block cells: 4 and 16 KiB are in the sweep but Section 2 commits to
    # keeping small-block sweeps internal, and they measure IOPS capability
    # rather than the ceiling this table is about.
    add_table(
        doc,
        ["Block size", "NVMe-oF XFS raid0", "NVMe-oF raw block",
         "Local target media"],
        [
            ["144 KiB", "95.7", "95.8", "107.8"],
            ["256 KiB", "95.9", "95.9", "107.7"],
            # The 512 KiB raw-block cell is the one number here that is not a
            # surface property, and it is captioned as such rather than hidden:
            # its three repetitions spanned 14.7 Gb/s, which is the generator
            # running out of offered concurrency (LMCache-3ce). XFS at the same
            # size holds 95.9, which is the check that says so.
            ["512 KiB", "95.9", "82.3", "107.8"],
        ],
        [1.20, 1.80, 1.75, 1.75],
    )
    add_caption(
        doc,
        "Table 1. Sustained read goodput in Gb/s, QD32, mean of three repetitions. "
        "95.9 Gb/s is 96% of 100 GbE nominal. The 512 KiB raw-block cell is the one "
        "outlier -- its repetitions spanned 14.7 Gb/s against under 0.2 elsewhere, so "
        "it is offered concurrency, not the surface; XFS holds 95.9 at that size."
    )
    add_body(
        doc,
        "What Table 1 does not establish is the host ceiling. Neither column comes "
        "near the target's memory or PCIe limit, and on two namespaces neither can -- "
        "that takes the drive-count axis in Table 2, the one row with no coverage at "
        "all.",
        keep_with_next=True,
    )
    # The coverage column is the point of this table. Given as a bare list of axes
    # it would read as a plan we might already have executed, and the honest
    # answer on four of the six rows is "partly" -- so each row states what the
    # 100 GbE rig actually produced next to what the full matrix asks for.
    add_table(
        doc,
        ["Axis", "Full matrix", "Covered on the 100 GbE rig"],
        [
            # The sweep's read cells run rw=read (sequential); random appears
            # only inside the randrw mixed cells. Write is absent standalone by
            # construction, not by omission: the corpus guard in
            # run_fio_capacity_sweep.sh refuses any write pattern on the
            # exported raw namespaces, so write appears only inside the mix.
            ["Access pattern", "seq read, seq write, rand read, rand write",
             "seq read; rand read and write only inside the mix"],
            ["Mixed R/W", "100% read, 100% write, 5:1",
             "100% read, 5:1, 9:1; no 100% write"],
            ["Block size", "64 KiB, 256 KiB, 512 KiB, 1 MiB",
             "256 and 512 KiB, plus 4, 16, 144 KiB"],
            # QD 1 and 4 are far below the knee and 256 is far above it; the rig
            # swept the range that contains the knee, which is 8 to 64.
            ["Queue depth", "1, 4, 16, 64, 256",
             "8 to 64 swept; three repetitions at 32"],
            ["Drive count", "4, 8, 16 namespaces (find the knee)",
             "2 namespaces, fixed -- no sweep possible"],
            # Config B is the substantive difference and is stated as such: a
            # same-host loopback removes the wire on purpose so the host is the
            # limiter, whereas our remote surface is a real cross-host link and
            # therefore caps at the link.
            ["Configs", "A local block; B NVMe-oF loopback, same host",
             "A local block; B is cross-host, not loopback"],
        ],
        [1.05, 2.45, 3.00],
    )
    add_caption(
        doc,
        "Table 2. The full storage-ceiling matrix against what the 100 GbE rig has "
        "produced. Both planned configs exclude the fabric on purpose, so the platform "
        "is the limiter; the measurements above do not, which is why they stop at 95.9."
    )
    add_body(
        doc,
        "Store runs in the same measured window at the requested 5:1 read:write ratio, "
        "verified byte-for-byte. Total goodput settles around 72 Gb/s once stores "
        "share the window, against 95.6 read-only."
    )
    add_figure(
        doc, "brief-store-and-retrieve",
        "Figure 2. LMCache bench l2, Mixtral 8x22B 256 KiB geometry, 32 workers, "
        "in-flight 8, 60 s window with readback verification. Concurrency is local "
        "multi-process, not physical multi-initiator."
    )
    add_body(
        doc,
        "That is not the link, and Table 3 is the cross-check. fio reaches within "
        "2.5% of the L2 mixed total with no LMCache in the path, so the cache client "
        "is not the primary limiter; and 9:1 "
        "barely shifts the total even though it cuts the write share almost in half, "
        "which a link limit would not do. Six RDMA fabric error counters stayed zero "
        "on every accepted run, with counter ratios inside 0.987-1.006. What "
        "saturates around 72 sits beneath the transport -- duplex behaviour on two "
        "drives, or filesystem writeback -- and two drives is too small a sample to "
        "separate those. We characterise it on the 16-drive target in the next step, "
        "where drive count is a variable.",
        keep_with_next=True,
    )
    # This table is the one place the brief leaves 256 KiB, and it has to: fio
    # ran mixed cells at 144 KiB only, and bench l2 has no 9:1 cells at all, so
    # neither cross-check exists at 256 KiB. It carries only the four cells that
    # make the not-the-link case -- both generators at one ratio, one generator
    # at a second ratio, and the read-only reference on the same surface. The
    # caption says why the block size differs so it does not read as a slip.
    add_table(
        doc,
        ["Workload", "Generator", "Read (Gb/s)", "Write (Gb/s)", "Total (Gb/s)"],
        [
            ["Read only", "fio", "95.7", "--", "95.7"],
            ["Mixed 5:1", "fio randrw, 144 KiB", "58.4", "12.0", "70.4"],
            [
                "Mixed 5:1",
                "bench l2, Mixtral, 4 local processes",
                "60.2",
                "12.0",
                "72.2",
            ],
            ["Mixed 9:1", "fio randrw", "65.5", "7.3", "72.7"],
        ],
        [1.15, 1.85, 1.15, 1.15, 1.20],
    )
    add_caption(
        doc,
        "Table 3. Goodput at 144 KiB, QD32, remote XFS raid0 over the Falcon link. "
        "The 5:1 fio row is the 2026-08-13 post-conditioning mean of three "
        "repetitions. 144 KiB rather than 256 because it is the only block size "
        "where both generators ran mixed cells; their concurrency structures remain "
        "different."
    )
    add_heading(doc, "Mixtral Geometry to L2 Benchmark Parameters", 2)
    add_table(
        doc,
        [
            "Mixtral 8x22B FP8 profile",
            "Resolved bench l2 parameters",
            "Per-process mixed configuration",
        ],
        [
            [
                "architecture:\n"
                "  num_layers: 56\n"
                "  num_kv_heads: 8\n"
                "  head_size: 128\n"
                "  kv_size: 2\n"
                "quantization:\n"
                "  dtype_bytes: 1  # FP8\n"
                "chunking:\n"
                "  tokens_per_chunk: 128",
                "2 KV x 8 heads x 128 values x 1 byte x 128 tokens\n"
                "= 262,144 bytes = 256 KiB per object\n\n"
                "56 layer objects x 256 KiB = 14 MiB per submit\n\n"
                "--kvcache-shape-profile mixtral_8x22b_fp8.yaml\n"
                "equivalent to --num-keys 56 --data-size-kb 256\n\n"
                "Mixed run: --in-flight 8 per process\n"
                "--duration-sec 60 --read-write-ratio 5:1",
                "Adapter: fs_native\n"
                "O_DIRECT; workers=8\n\n"
                "lmcache bench l2\n"
                "--kvcache-shape-profile\n"
                "  mixtral_8x22b_fp8.yaml\n"
                "--key-prefix <read>\n"
                "--write-key-prefix <write>\n"
                "--read-write-ratio 5:1\n"
                "--in-flight 8\n"
                "--duration-sec 60\n"
                "--l1-align-bytes 4096\n"
                "--no-skip-verify",
            ]
        ],
        [2.17, 2.17, 2.16],
        body_font_name="Consolas",
        body_font_size=8.0,
    )
    add_caption(
        doc,
        "The tested profile uses 128-token pages; 256-token pages would resolve to "
        "512 KiB objects and 28 MiB submits."
    )
    add_body(
        doc,
        "Table 4 collects the remaining headline cells, all at 256 KiB. LMCache bench "
        "l2 comes within 0.3 Gb/s of Table 1's fio ceiling and two model page "
        "geometries agree to within 0.05, so the number belongs to the path, not to "
        "one generator or page layout.",
        keep_with_next=True,
    )
    # The "Measured by" column names the generator and the surface for every
    # row, because the table mixes two generators -- fio and LMCache bench l2 --
    # and a reader cannot otherwise tell which number is an NVMe-level result
    # and which is the cache client's. Every row is remote over the Falcon link.
    add_table(
        doc,
        ["Workload cell", "Metric", "Measured by", "Result"],
        [
            # No goodput row for fio at 256 KiB: Table 1 carries that cell, and
            # repeating 95.9 here would be a mirror. Latency is the part Table 1
            # does not report, so this row keeps it and drops the throughput.
            ["256 KiB, read", "Read latency, mean / p99",
             "fio, remote XFS raid0", "0.69 / 0.96 ms"],
            # No target-local row here: Figure 1's whole point is the local-vs-
            # remote ladder, so repeating 107.7 in the table would mirror it.
            # Two profiles, one row: the point is that they agree, so listing
            # them separately would spend a row saying the same thing twice.
            ["256 KiB, read", "Two model page geometries",
             "bench l2, remote XFS raid0",
             "95.60 and 95.64 Gb/s (Mixtral 8x22B, Llama-3 405B)"],
            ["Mixed 5:1", "Sustained goodput, 4 instances",
             "bench l2, remote XFS raid0",
             "72.7 Gb/s total: 60.6 read + 12.1 write"],
            # Both remaining rows are 256 KiB. The mixed p99 and the QD8 depth
            # point were 144 KiB fio cells; a table headed "all at 256 KiB"
            # cannot carry them, and 256 KiB has no depth sweep of its own --
            # the probe_qd*.json artifacts are all 144k. So the depth claim is
            # made from the two 256 KiB depths that do exist, and the mixed p99
            # is dropped rather than labelled with a second block size.
            ["Depth", "Goodput, QD16 -> QD32", "fio, remote XFS raid0",
             "93.2 -> 95.9 Gb/s; capacity-bound at QD32"],
            ["Drive count", "Goodput vs drive count", "not yet run",
             "2 drives today; gated on MMG-400 hardware"],
        ],
        [1.05, 1.55, 1.55, 2.35],
    )
    add_caption(
        doc,
        "Table 4. Headline cells at 256 KiB, QD32, remote XFS raid0 over the Falcon "
        "link. fio rows are the mean of three accepted repetitions."
    )
    # The depth curve is cut: its probes are 144 KiB, and this brief is 256 KiB
    # throughout. Table 4's depth row carries the claim from 256 KiB cells.
    # brief_charts.py still renders it for internal use.
    add_figure(
        doc, "brief-setup-flows",
        "Figure 3. Current setup -- the rig behind every number above. The target "
        "exports both PM9A3s raw; the initiator owns md0 and XFS. MEV-100 IPU on "
        "each host, carrying Falcon transport only."
    )
    add_body(
        doc,
        "The next step is the same harness on MMG-400 at 400 GbE (Figure 4). Nothing "
        "about the flows changes; the link, the IPU count, and the drive count do. "
        "Phase 1 target is 1x400 GbE at 256 KiB, 100% read, 64 aggregate queue pairs, "
        "sustaining at least 45 GB/s. One change is ours to make: page size sets the "
        "object size today, but the current path re-segments to about 51 KiB per "
        "transfer before the wire, so landing 128-256 KiB sends on the fabric is work "
        "for the 400 GbE phase rather than something the numbers above already show. "
        "Timing follows the hardware: 4x400 needs the target to sustain 180 GB/s of "
        "local block capacity, which gates on drive count, so we will confirm the "
        "window before committing to a date."
    )
    add_figure(
        doc, "brief-mmg400-setup",
        "Figure 4. Next step -- N independent initiators, four MMG-400 IPUs and 16 "
        "Gen5 NVMe drives on one dual-socket target. Phase 1 lights one 400 GbE link; "
        "the follow-on scales across all four."
    )

    # --- 2. Framing -------------------------------------------------------
    # The break sits here rather than before Section 3. Section 1 now ends flush
    # with the bottom of its last page, so left to flow this heading strands at
    # the foot of it with the scope callout on the next page. Breaking here puts
    # both closing sections on one page together and neither one splits.
    add_heading(doc, "2. How we want to run the PoC", 1, page_break_before=True)
    add_scope_note(
        doc, "Scope",
        "We are not building a KV cache. You have one. What we want to demonstrate is "
        "that your retrieve and store flows run over Falcon-offloaded NVMe-oF at line "
        "rate, using standard kernel transport and no Intel software in the data path. "
        "The deliverable is performance evidence and representative I/O flows, not a "
        "cache product."
    )
    add_body(
        doc,
        "That framing is what keeps the PoC small enough for us to finish on the "
        "MMG-400 window. Three consequences follow, and each one removes work:"
    )
    add_bullet(
        doc,
        "kernel nvme-rdma on the initiator, nvmet-rdma on the target exporting raw "
        "namespaces. The initiator owns the md0 stripe and XFS. No SPDK, no custom "
        "target, no cache logic on the IPU -- it carries Falcon transport only. "
        "Payload stages through host DRAM; controller memory buffers stay out of "
        "phase 1.",
        lead="Stock transport, both ends."
    )
    add_bullet(
        doc,
        "LMCache bench l2 replays the two flows at real model page geometry with "
        "byte verification. It is a load generator that produces the right I/O "
        "shape; it is not a proposed integration point.",
        lead="A benchmark harness, not an integration."
    )
    add_bullet(
        doc,
        "read-heavy retrieve at model page size, plus a 5:1 mixed window that "
        "exercises store concurrently. That is enough to characterise the path. "
        "Small-block and queue-depth sweeps stay internal.",
        lead="Two workloads, not a matrix."
    )

    # --- 3. Opens ---------------------------------------------------------
    # Asks only. The 5:1 shortfall and the MMG-400 date are ours to close, so they
    # sit with the results and the next-step paragraph in Section 1 rather than
    # here -- a question we answer ourselves is not a question for the reader.
    # No break before this one: it follows Section 2 on the same page, which the
    # break above reserves for the two of them.
    add_heading(doc, "3. Questions for you", 1)
    add_body(
        doc,
        "Five decisions we need from your side before the MMG-400 run is worth "
        "scheduling. Everything else above is ours to close."
    )
    add_bullet(
        doc,
        "we can report host CPU reduction at equal throughput, p99 for small I/O, "
        "sustained throughput for large I/O, or time-to-online after a cold restart. "
        "We would rather measure the two you gate on than report all four thinly.",
        lead="Which metrics decide this for you?"
    )
    add_bullet(
        doc,
        "the concurrency above is local multi-process on one initiator: it proves "
        "concurrency, not fabric fan-in. A physical result needs a synchronized "
        "multi-host driver we have not built. Is that required, or is "
        "single-initiator line rate enough?",
        lead="Do you need a physical multi-initiator result?"
    )
    add_bullet(
        doc,
        "phase 1 stages payload through target host DRAM. Placing it in SSD "
        "controller memory buffers instead would cut a DRAM crossing, but it "
        "constrains drive selection and adds target-side work. Is that a direction "
        "you want characterised, or out of scope?",
        lead="Do controller memory buffers matter to you?"
    )
    add_bullet(
        doc,
        "we assume one namespace per drive, disjoint per initiator, with no shared "
        "metadata. If your cache expects a different mapping of clients to "
        "namespaces and drives, that changes the target layout.",
        lead="How should initiators map to namespaces and drives?"
    )
    add_bullet(
        doc,
        "256 KiB read at model page geometry, plus the 5:1 mixed window. Is that the "
        "shape your retrieve and store traffic actually takes, or should we match a "
        "different page size and ratio?",
        lead="Are these the right I/O shapes?"
    )

    return doc


def main() -> None:
    """Parse arguments and write the brief."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, default=REPO_ROOT,
                        help="directory to write the .docx into")
    args = parser.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    output = args.outdir / OUTPUT_NAME
    build().save(output)
    print(output.resolve())


if __name__ == "__main__":
    main()
