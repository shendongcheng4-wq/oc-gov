"""公文 docx 生成：纯标准库拼一个 Word 能直接打开的 .docx。

为什么不用 python-docx
---------------------
这个项目从第一天起就是「标准库即可跑」：`docparse.py` 连 xlsx / docx 的**解析**
都是手写的 zip + XML，导出反过来去引一个第三方库，等于给「不联网也能跑」开个口子。
更要紧的是，真正难的部分从来不是写文件，而是**样式映射**——仿宋_GB2312 三号、
行距 28.5 磅、页边距上 37 下 35 左 28 右 26mm——这部分换哪个库都得自己配一遍。

docx 的骨架
----------
一个 zip，四类部件：

    [Content_Types].xml        每种扩展名/部件声明 MIME
    _rels/.rels                包级关系，指向 word/document.xml
    word/document.xml          正文（本模块唯一由内容驱动的那份）
    word/styles.xml            默认字体字号行距
    word/footer1.xml           页脚（页码）
    docProps/*.xml             标题作者，Word 的「属性」里能看到

单位换算（写错任何一处，Word 就把文件判成损坏）
------------------------------------------------
- 长度：1 毫米 = 56.6929 twips（1 twip = 1/20 磅，1 磅 = 1/72 英寸）
- 字号：`w:sz` 是**半点**，三号 16 磅 → 32；二号 22 磅 → 44
- 行距：`w:line` 也是 twips，28.5 磅 → 570

元素顺序
--------
OOXML 的 schema 是 sequence，`w:pPr` 里 `spacing` 必须写在 `ind` 前、`jc` 写在
`ind` 后；`w:rPr` 里 `rFonts` 必须最靠前。顺序错了 Word 不报错，直接丢格式，
排查起来极其费劲——下面每个函数都按 schema 顺序拼，别随手插。

明确没做
--------
- 不写图片（公文红头如果需要图片版机关名，那是另一个话题）
- 不写修订、批注、目录、脚注
- 不处理分栏、竖排、信封版式
"""

from __future__ import annotations

import base64
import io
import zipfile
from datetime import datetime, timezone
from typing import Any, Sequence

# ---------------------------------------------------------------- 单位与常量

TWIPS_PER_MM = 56.6929


def _mm(value: float) -> int:
    """毫米 → twips。"""
    return int(round(value * TWIPS_PER_MM))


# 公文常用字号（半点）
SZ_2 = 44      # 二号 22 磅：标题、发文机关标志
SZ_3 = 32      # 三号 16 磅：正文
SZ_4 = 28      # 四号 14 磅：页码
SZ_S4 = 24     # 小四 12 磅：表格
SZ_5 = 21      # 五号 10.5 磅：表格里的密排

FONT_BODY = "仿宋_GB2312"
FONT_TITLE = "方正小标宋简体"
FONT_HEI = "黑体"
FONT_KAI = "楷体_GB2312"
FONT_SONG = "宋体"
FONT_LATIN = "Times New Roman"

# A4，纵向。公文默认版心：天头 37、地脚 35、订口 28、切口 26（毫米）
PAGE_W, PAGE_H = 11906, 16838
MARGIN_TOP, MARGIN_BOTTOM, MARGIN_LEFT, MARGIN_RIGHT = 37, 35, 28, 26

# 正文行距：公文每页 22 行，28.5 磅 ≈ 570 twips
LINE_BODY = 570


def _esc(text: Any) -> str:
    """XML 转义。制表符转空格——公文里没人用真 tab，留着会在 Word 里跑版。"""
    s = str(text if text is not None else "")
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace('"', "&quot;").replace("\t", "    ")
    return s


def _runs(text: str) -> list[str]:
    """按换行拆成多个 run。Word 里段内换行是 `<w:br/>`，不是新段落。"""
    return text.split("\n")


# ---------------------------------------------------------------- 片段拼装

def _rpr(
    *,
    size: int = SZ_3,
    font: str = FONT_BODY,
    bold: bool = False,
    color: str | None = None,
    spacing: int | None = None,
) -> str:
    """rPr 片段。子元素顺序按 schema：rFonts → b → color → spacing → sz → szCs。"""
    out = [
        f'<w:rFonts w:ascii="{FONT_LATIN}" w:eastAsia="{font}" '
        f'w:hAnsi="{FONT_LATIN}" w:cs="{FONT_LATIN}"/>'
    ]
    if bold:
        out.append("<w:b/><w:bCs/>")
    if color:
        out.append(f'<w:color w:val="{color}"/>')
    if spacing:
        # 字符间距，公文里的发文机关标志要撑开一点
        out.append(f'<w:spacing w:val="{spacing}"/>')
    out.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    return "".join(out)


def _run(text: str, **kw: Any) -> str:
    """一个文本 run。空文本也给一个空 run —— 保留行高。"""
    rpr = _rpr(**kw)
    if not text:
        return f"<w:r><w:rPr>{rpr}</w:rPr></w:r>"
    lines = _runs(text)
    body = "<w:br/>".join(f'<w:t xml:space="preserve">{_esc(x)}</w:t>' for x in lines)
    return f"<w:r><w:rPr>{rpr}</w:rPr>{body}</w:r>"


def _ppr(
    *,
    align: str | None = None,
    first_line_chars: int | None = None,
    size: int = SZ_3,
    line: int | None = None,
    before: int = 0,
    after: int = 0,
    border_bottom: tuple[str, int, int] | None = None,
    keep_next: bool = False,
    outline: int | None = None,
) -> str:
    """pPr 片段。顺序：keepNext → pBdr → spacing → ind → jc → outlineLvl。"""
    out: list[str] = []
    if keep_next:
        out.append("<w:keepNext/>")
    if border_bottom:
        color, sz, space = border_bottom
        out.append(
            "<w:pBdr>"
            f'<w:bottom w:val="single" w:sz="{sz}" w:space="{space}" w:color="{color}"/>'
            "</w:pBdr>"
        )
    if line or before or after:
        bits = ['w:before="%d"' % before if before else "",
                'w:after="%d"' % after if after else ""]
        # lineRule="exact" 是公文的关键：行距固定，插图/大字才不会把行撑开
        if line:
            bits.append('w:line="%d"' % line)
            bits.append('w:lineRule="exact"')
        out.append("<w:spacing %s/>" % " ".join(b for b in bits if b))
    if first_line_chars:
        # firstLineChars 是 Word 的「字符」单位（100 = 1 字符），firstLine 是兜底
        out.append(
            f'<w:ind w:firstLineChars="{first_line_chars}" '
            f'w:firstLine="{int(size / 2 * 20 * first_line_chars / 100)}"/>'
        )
    if align:
        out.append(f'<w:jc w:val="{align}"/>')
    if outline is not None:
        out.append(f"<w:outlineLvl w:val=\"{outline}\"/>")
    return "".join(out)


class Doc:
    """一份文档。按顺序 add 段落/表格，最后 build() 出字节。"""

    def __init__(self, *, title: str = "", author: str = "", subject: str = "") -> None:
        self.blocks: list[str] = []
        # 注意不能叫 self.title：那个名字被 title() 方法占了，属性会把它盖掉
        self._title = title
        self._author = author
        self.subject = subject

    # ---- 段落 ----

    def para(
        self,
        text: str = "",
        *,
        segments: Sequence[tuple[str, bool]] | None = None,
        size: int = SZ_3,
        font: str = FONT_BODY,
        bold: bool = False,
        color: str | None = None,
        align: str | None = None,
        indent: bool = True,
        line: int | None = LINE_BODY,
        before: int = 0,
        after: int = 0,
        border_bottom: tuple[str, int, int] | None = None,
        keep_next: bool = False,
        outline: int | None = None,
        char_spacing: int | None = None,
    ) -> None:
        """一段。segments 用于「一、标题。正文」这种前半句加粗的公文写法。

        图省事时可以直接把 segments 当第一个参数传进来（[(文本, 是否加粗), ...]）。
        """
        if segments is None and isinstance(text, (list, tuple)):
            segments, text = text, ""
        ppr = _ppr(
            align=align,
            first_line_chars=200 if indent else None,
            size=size,
            line=line,
            before=before,
            after=after,
            border_bottom=border_bottom,
            keep_next=keep_next,
            outline=outline,
        )
        if segments:
            runs = "".join(
                _run(t, size=size, font=font, bold=b, color=color, spacing=char_spacing)
                for t, b in segments
            )
        else:
            runs = _run(text, size=size, font=font, bold=bold, color=color,
                        spacing=char_spacing)
        self.blocks.append(f"<w:p><w:pPr>{ppr}</w:pPr>{runs}</w:p>")

    def blank(self, *, line: int = LINE_BODY, size: int = SZ_3) -> None:
        self.para("", size=size, indent=False, line=line)

    def title(self, text: str, *, size: int = SZ_2, after: int = 240) -> None:
        """公文标题：二号小标宋，居中，可折行。"""
        self.para(text, size=size, font=FONT_TITLE, align="center", indent=False,
                  line=None, after=after, keep_next=True, outline=0)

    def red_head(self, org: str) -> None:
        """发文机关标志：红色二号小标宋，居中，字间撑开。"""
        self.para(org, size=SZ_2, font=FONT_TITLE, color="FF0000", align="center",
                  indent=False, line=None, before=240, after=120, char_spacing=40)

    def red_line(self) -> None:
        """红色反线：公文红头下面那条线。用一个空段落的下边框画，比画表格稳。"""
        self.para("", indent=False, line=120, border_bottom=("FF0000", 12, 1))

    def right(self, text: str, *, after: int = 0) -> None:
        """右对齐（落款、日期）。按公文习惯右侧留 2 字。"""
        self.para(text, align="right", indent=False, after=after)

    def page_break(self) -> None:
        self.blocks.append(
            '<w:p><w:pPr><w:spacing w:line="20" w:lineRule="exact"/></w:pPr>'
            '<w:r><w:br w:type="page"/></w:r></w:p>'
        )

    # ---- 表格 ----

    def table(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        widths: Sequence[int] | None = None,
        size: int = SZ_5,
        align_right: Sequence[int] = (),
    ) -> None:
        """三线表之外的最简表格：全框线 + 表头浅灰底。

        widths 是百分比（合计 100）；不传就均分。数字列用 align_right 指定，
        让小数点能对齐——公文附表里这一条最影响观感。
        """
        ncol = len(columns)
        if ncol == 0:
            return
        usable = PAGE_W - _mm(MARGIN_LEFT) - _mm(MARGIN_RIGHT)
        if widths is None or len(widths) != ncol:
            widths = [100 / ncol] * ncol
        cols = [max(1, int(usable * w / sum(widths))) for w in widths]

        border = (
            '<w:tblBorders>'
            '<w:top w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
            '<w:left w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
            '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
            '<w:right w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
            '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
            '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
            '</w:tblBorders>'
        )
        out = [
            "<w:tbl><w:tblPr>",
            '<w:tblW w:w="5000" w:type="pct"/>',
            border,
            '<w:tblLayout w:type="fixed"/>',
            '<w:tblCellMar><w:top w:w="28" w:type="dxa"/><w:left w:w="85" w:type="dxa"/>'
            '<w:bottom w:w="28" w:type="dxa"/><w:right w:w="85" w:type="dxa"/></w:tblCellMar>',
            "</w:tblPr><w:tblGrid>",
        ]
        out.extend(f'<w:gridCol w:w="{c}"/>' for c in cols)
        out.append("</w:tblGrid>")

        def _cell(text: Any, i: int, *, head: bool) -> str:
            jc = "right" if (i in align_right and not head) else ("center" if head else "left")
            shd = '<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>' if head else ""
            ppr = (
                f'<w:pPr><w:spacing w:line="240" w:lineRule="auto" w:before="20" w:after="20"/>'
                f'<w:jc w:val="{jc}"/><w:rPr>{_rpr(size=size, font=FONT_SONG, bold=head)}</w:rPr></w:pPr>'
            )
            return (
                f'<w:tc><w:tcPr><w:tcW w:w="{cols[i]}" w:type="dxa"/>{shd}'
                '<w:vAlign w:val="center"/></w:tcPr>'
                f'<w:p>{ppr}{_run(str(text if text is not None else ""), size=size, font=FONT_SONG, bold=head)}</w:p></w:tc>'
            )

        out.append("<w:tr><w:trPr><w:tblHeader/></w:trPr>")
        out.extend(_cell(c, i, head=True) for i, c in enumerate(columns))
        out.append("</w:tr>")
        for row in rows:
            out.append("<w:tr>")
            out.extend(_cell(row[i] if i < len(row) else "", i, head=False)
                       for i in range(ncol))
            out.append("</w:tr>")
        out.append("</w:tbl>")
        self.blocks.append("".join(out))
        # 表格后面必须有段落，否则 Word 会在表尾补一个空段，两页之间出现空白页
        self.para("", indent=False, line=120, size=SZ_5)

    # ---- 输出 ----

    def build(self) -> bytes:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sect = (
            "<w:sectPr>"
            '<w:footerReference w:type="default" r:id="rId2"/>'
            f'<w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/>'
            f'<w:pgMar w:top="{_mm(MARGIN_TOP)}" w:right="{_mm(MARGIN_RIGHT)}" '
            f'w:bottom="{_mm(MARGIN_BOTTOM)}" w:left="{_mm(MARGIN_LEFT)}" '
            'w:header="851" w:footer="992" w:gutter="0"/>'
            '<w:cols w:space="425"/>'
            "</w:sectPr>"
        )
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<w:body>{''.join(self.blocks)}{sect}</w:body></w:document>"
        )
        return _package(document, self._styles(), _footer(), self._title, self._author, now)

    def _styles(self) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:docDefaults><w:rPrDefault><w:rPr>"
            f'<w:rFonts w:ascii="{FONT_LATIN}" w:eastAsia="{FONT_BODY}" '
            f'w:hAnsi="{FONT_LATIN}" w:cs="{FONT_LATIN}"/>'
            f'<w:sz w:val="{SZ_3}"/><w:szCs w:val="{SZ_3}"/>'
            "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>"
            f'<w:spacing w:line="{LINE_BODY}" w:lineRule="exact"/>'
            "</w:pPr></w:pPrDefault></w:docDefaults>"
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
            '<w:name w:val="Normal"/><w:qFormat/></w:style>'
            "</w:styles>"
        )


def _footer() -> str:
    """页脚：公文页码用 4 号宋体，数字两侧各一条一字线。居中排，不做奇偶分外侧。"""
    page = (
        "<w:r><w:rPr>" + _rpr(size=SZ_4, font=FONT_SONG) + "</w:rPr>"
        '<w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:rPr>" + _rpr(size=SZ_4, font=FONT_SONG) + "</w:rPr>"
        '<w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        "<w:r><w:rPr>" + _rpr(size=SZ_4, font=FONT_SONG) + "</w:rPr>"
        '<w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:rPr>" + _rpr(size=SZ_4, font=FONT_SONG) + "</w:rPr><w:t>1</w:t></w:r>"
        "<w:r><w:rPr>" + _rpr(size=SZ_4, font=FONT_SONG) + "</w:rPr>"
        '<w:fldChar w:fldCharType="end"/></w:r>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        + _run("— ", size=SZ_4, font=FONT_SONG)
        + page
        + _run(" —", size=SZ_4, font=FONT_SONG)
        + "</w:p></w:ftr>"
    )


def _package(document: str, styles: str, footer: str,
             title: str, author: str, now: str) -> bytes:
    """把五份 XML 打进一个 zip。顺序不重要，[Content_Types].xml 放第一个是习惯。"""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
        "</Relationships>"
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_esc(title)}</dc:title>"
        f"<dc:creator>{_esc(author or 'oc-gov')}</dc:creator>"
        f"<cp:lastModifiedBy>{_esc(author or 'oc-gov')}</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )
    app = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>oc-gov</Application><AppVersion>0.2</AppVersion>"
        "</Properties>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", rels),
            ("word/document.xml", document),
            ("word/styles.xml", styles),
            ("word/footer1.xml", footer),
            ("word/_rels/document.xml.rels", doc_rels),
            ("docProps/core.xml", core),
            ("docProps/app.xml", app),
        ):
            zf.writestr(name, data.encode("utf-8"))
    return buf.getvalue()


def to_base64(data: bytes) -> str:
    """接口回传用。浏览器侧 atob → Uint8Array → Blob 下载。"""
    return base64.b64encode(data).decode("ascii")
