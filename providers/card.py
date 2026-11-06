"""HTML rendering of an operator card.

The markup and styles follow the AmiyaBot ``operatorArchives`` template
(``template/operatorInfo.html`` + ``operatorInfo.css`` + ``css/skillType.css``):
an operator background, the illustration as a translucent layer, a left column
with attribute cells, potential ranks, real name and voice actors, the range
grid, class icon, tags, trait and name block, and a right column with the
record, token, talents, base skills and skill rows.

The template is handed to a remote render service with an empty context, so
every value is filled here and the styles live in one ``<style>`` block.
"""

from __future__ import annotations

import base64
import io
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from ..models import SafeHtml

#: Decorative background shipped with the AmiyaBot template.
_BACKGROUND_FILE = Path(__file__).resolve().parent.parent / "assets" / "operator_bg.png"


@lru_cache(maxsize=1)
def _background() -> str:
    """Encode the bundled template background as a data URI.

    The render service cannot read plugin files and the upstream GitHub URL is
    not reachable from every host, so the texture travels inside the template.

    Returns:
        A ``data:`` URI, or a flat gradient when the asset is missing.
    """
    try:
        payload = _BACKGROUND_FILE.read_bytes()
    except OSError:
        return "linear-gradient(135deg, #101010 0%, #ededed 100%)"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/png;base64,{encoded}"


#: Potential badges shipped with the AmiyaBot template, ``rank/1.png`` … ``6.png``.
_RANK_DIR = Path(__file__).resolve().parent.parent / "assets" / "rank"


@lru_cache(maxsize=8)
def _rank_icon(rank: int) -> str:
    """Encode a potential rank badge as a data URI.

    Args:
        rank: Zero-based potential rank, matching ``potentialRanks`` order.

    Returns:
        A ``data:`` URI, or an empty string when the badge is missing.
    """
    try:
        payload = (_RANK_DIR / f"{rank + 1}.png").read_bytes()
    except OSError:
        return ""
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


_STYLE = """
* { font-size: 18px; box-sizing: border-box;
    font-family: 'Noto Sans CJK SC', 'Source Han Sans SC', 'Microsoft YaHei',
    'PingFang SC', sans-serif; }
body { margin: 0; }
#template { width: 1400px; background: url(BACKGROUND_URL) center / cover no-repeat;
    overflow: hidden; position: relative; }
.background { width: 100%; padding: 10px; background-color: rgb(255 255 255 / 30%);
    background-position: bottom 25px left 25%;
    background-size: auto calc(100% - 200px); background-repeat: no-repeat;
    display: flex; justify-content: space-between; }
.left { width: 50%; min-height: 800px; display: flex; flex-direction: column;
    justify-content: space-between; }
.right { width: 47%; display: flex; flex-direction: column;
    justify-content: space-between; }
.operator-attr { display: flex; flex-wrap: wrap; font-size: 20px; }
.operator-attr .attr { width: calc(100% / 3); margin-bottom: 5px;
    background: rgb(236 236 236 / 80%); display: flex; align-items: center; }
.operator-attr .attr > div { width: calc(100% - 110px); }
.operator-attr .attr span.a { color: #0069ff; }
.operator-attr .attr span.b { color: #ff641e; }
.operator-attr .attr:before { content: attr(title); width: 100px;
    padding: 2px 10px; margin-right: 10px; display: inline-block;
    background: #000; color: #fff; }
.operator-name { margin-top: 30px; display: flex; flex-direction: column; }
.operator-name .name { border-radius: 2px; background: rgb(255 255 255 / 50%);
    padding: 0 10px; display: flex; align-items: center; }
.operator-name .name > span { color: #fff; background: #000; padding: 5px 10px;
    border-radius: 4px; margin-right: 10px; }
.class-info { display: flex; height: 60px; }
.rarity { display: flex; flex-direction: column; justify-content: center;
    padding-left: 10px; background: rgb(255 255 255 / 50%); }
.tags { margin: 10px 0; }
.tags span { padding: 5px 10px; margin-right: 5px; background: #000;
    color: #fff; border-radius: 4px; }
.trait { font-size: 16px; padding: 10px; margin-bottom: 10px; display: flex;
    align-items: center; background: rgb(255 255 255 / 50%); }
.skill-item { border-radius: 2px; padding: 15px 5px;
    background: rgb(48 48 48 / 75%); display: flex; color: #fff; }
.skill-item:not(:last-child) { margin-bottom: 5px; }
.skill-type { display: flex; }
.skill-type > div { margin-right: 5px; background: #737373; padding: 3px 8px;
    border-radius: 4px; white-space: nowrap; }
.skill-type > .tINCREASE_WITH_TIME,
.skill-type > .t1 { background: #98c043 !important; }
.skill-type > .tINCREASE_WHEN_ATTACK,
.skill-type > .tINCREASE_WHEN_TAKEN_DAMAGE,
.skill-type > .t2,
.skill-type > .t4 { background: #ec7e4c !important; }
.skill-range { width: 120px; display: flex; align-items: center;
    justify-content: center; }
.skill-desc { font-size: 16px !important; }
.mark { color: #00b3ff; }
.block-group { display: flex; }
.block { width: 15px; height: 15px; margin: 1px; }
.block.r { border: 1px solid #000; background: #fff; }
.block.s { background: #000; }
.range .block.r { border: 2px solid #000; }
.title { color: #fff; background: #000; padding: 2px 10px; margin-bottom: 5px; }
.exInfo { display: flex; align-items: flex-start; padding: 2px 10px;
    background: rgb(255 255 255 / 50%); }
.exInfo.bs > div { padding-top: 5px; }
.exInfo:last-child { margin-bottom: 10px; }
.potential { display: flex; }
.bs-icon { width: 30px; height: 30px; margin: 0 5px;
    background: center / 100% no-repeat; }
"""


def _style(font: str | None) -> str:
    """Assemble the stylesheet, optionally pinning the bundled font.

    Args:
        font: Font file name referenced relatively; ``None`` keeps the
            fallback stack.

    Returns:
        The stylesheet of the card.
    """
    style = _STYLE.replace("BACKGROUND_URL", _background())
    if font:
        style += (
            f"@font-face {{ font-family: 'Harmony'; src: url('{font}') "
            "format('truetype'); }\n"
            "* { font-family: 'Harmony', serif !important; }\n"
        )
    return style


#: Dark background of the device page shipped with the AmiyaBot template.
_TOKEN_BACKGROUND_FILE = (
    Path(__file__).resolve().parent.parent / "assets" / "pc_bg.jpeg"
)


@lru_cache(maxsize=1)
def _token_background() -> str:
    """Encode the device page background as a data URI.

    Returns:
        A ``data:`` URI, or a dark gradient when the asset is missing.
    """
    try:
        payload = _TOKEN_BACKGROUND_FILE.read_bytes()
    except OSError:
        return "linear-gradient(135deg, #0b0b0b 0%, #2a2a2a 100%)"
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")


#: Stylesheet of the device page, ported from ``operatorToken.css``.
_TOKEN_STYLE = """
* { font-size: 18px; box-sizing: border-box;
    font-family: 'Noto Sans CJK SC', 'Source Han Sans SC', 'Microsoft YaHei',
    'PingFang SC', sans-serif; }
body { margin: 0; }
#template { width: 1280px; min-height: 720px; padding: 20px; color: #fff;
    background: url(TOKEN_BACKGROUND_URL) center / cover no-repeat;
    position: relative; }
.title { font-size: 40px; display: flex; align-items: center; margin-bottom: 30px; }
.title > img { width: 80px; border-radius: 50%; margin-right: 20px; }
.tokenItem:not(:last-child) { margin-bottom: 80px; }
.tokenInfo { display: flex; align-items: center; margin-bottom: 10px; }
.tokenInfo img { width: 100px; }
.block-group { display: flex; }
.block { width: 20px; height: 20px; margin: 2px; }
.block.r { border: 1px solid #fff; }
.block.s { background: #fff; }
table { border-collapse: collapse; border-spacing: 0; }
table th, table td { padding: 5px 10px; border: 1px solid #5c5c5c; }
table th { white-space: nowrap; text-align: left; }
.tag { width: fit-content; background: white; color: black; padding: 0 10px;
    margin-bottom: 10px; }
.mark { color: #00b3ff; }
.skill-type { display: flex; }
.skill-type > div { margin-right: 5px; background: #737373; padding: 3px 8px;
    border-radius: 4px; white-space: nowrap; }
.skill-type > .tINCREASE_WITH_TIME,
.skill-type > .t1 { background: #98c043 !important; }
.skill-type > .tINCREASE_WHEN_ATTACK,
.skill-type > .tINCREASE_WHEN_TAKEN_DAMAGE,
.skill-type > .t2,
.skill-type > .t4 { background: #ec7e4c !important; }
"""


def _token_style(font: str | None) -> str:
    """Assemble the device page stylesheet.

    Args:
        font: Font file name referenced relatively; ``None`` keeps the
            fallback stack.

    Returns:
        The stylesheet of the device page.
    """
    style = _TOKEN_STYLE.replace("TOKEN_BACKGROUND_URL", _token_background())
    if font:
        style += (
            f"@font-face {{ font-family: 'Harmony'; src: url('{font}') "
            "format('truetype'); }\n"
            "* { font-family: 'Harmony', serif !important; }\n"
        )
    return style


def render_token_card(card: dict[str, Any], *, font: str | None = None) -> str:
    """Build the device page of one operator.

    The page follows ``template/operatorToken.html``: a round avatar and
    ``<name> 的召唤物`` header, then one block per device with its stats table,
    attack range, talents and skills.

    Args:
        card: Mapping produced by a provider's ``token_card`` method.
        font: Font file name the document may reference relatively.

    Returns:
        A complete HTML document for the device page.
    """
    header_avatar = _icon(card.get("avatar") or "", "")
    tokens = ""
    for token in card.get("tokens") or []:
        pill = ""
        if token.get("type"):
            colour = escape(token.get("type_color") or "#9c27b0")
            pill = (
                '<span style="padding:3px 10px;border-radius:4px;'
                f'background-color:{colour}">{escape(token["type"])}</span>'
            )
        rows = token.get("attributes") or []
        table = (
            "<table>"
            + "".join(
                "<tr>"
                + "".join(
                    f"<td>{escape(label)}</td><td>{escape(value)}</td>"
                    for label, value in rows[index : index + 2]
                )
                + "</tr>"
                for index in range(0, len(rows), 2)
            )
            + "</table>"
            if rows
            else ""
        )
        talents = "".join(
            '<div style="margin-bottom:10px">'
            f'<div style="color:#ff9800;margin-bottom:5px">{escape(item["name"])}</div>'
            f"<div>{_text(item['text'])}</div></div>"
            for item in token.get("talents") or []
        )
        skills = ""
        for item in token.get("skills") or []:
            chips = ""
            if item.get("sp_type_name"):
                chips += (
                    f'<div class="t{escape(item["sp_type"])}">'
                    f"{escape(item['sp_type_name'])}</div>"
                )
            if item.get("skill_type_name"):
                chips += f"<div>{escape(item['skill_type_name'])}</div>"
            if item.get("sp_cost"):
                chips += f"<div>技力 {escape(item['sp_init'])}/{escape(item['sp_cost'])}</div>"
            if float(item.get("duration") or 0) > 0:
                chips += f"<div>持续 {escape(item['duration'])} 秒</div>"
            skills += (
                '<div style="margin-bottom:20px">'
                '<div style="margin-bottom:5px;display:flex;align-items:center">'
                '<div style="color:#00BCD4;margin-bottom:5px;margin-right:20px">'
                f"{escape(item['name'])}</div>"
                f'<div class="skill-type">{chips}</div></div>'
                f"<div>{_text(item['text'])}</div></div>"
            )
        tokens += (
            '<div class="tokenItem">'
            '<div class="tokenInfo">'
            f'<div style="margin-right:20px">{_icon(token.get("avatar") or "", "")}'
            "</div><div><div>"
            f'<span style="font-size:30px">{escape(token["name"])}</span>'
            f'<span style="padding:0 10px">{escape(token.get("en_name") or "")}</span>'
            f"{pill}</div>"
            '<div style="color:#FFEB3B;margin-top:10px">'
            f"{_text(token.get('description') or '')}</div></div></div>"
            f"{table}"
            '<div style="margin:20px 0"><div class="tag">攻击范围</div>'
            f"{_blocks(token.get('range') or [])}</div>"
            + (
                f'<div style="margin:20px 0"><div class="tag">天赋</div>{talents}</div>'
                if talents
                else ""
            )
            + (
                '<div style="margin-top:20px"><div class="tag">技能</div>'
                f"{skills}</div>"
                if skills
                else ""
            )
            + "</div>"
        )

    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        f"<style>{_token_style(font)}</style></head><body>"
        f'<div id="template"><div class="title">{header_avatar}'
        f"{escape(card['name'])} 的召唤物</div>{tokens}</div></body></html>"
    )


def encode_art(payload: bytes, size: int = 700) -> str:
    """Encode a character illustration as a downscaled JPEG data URI.

    The card paints the artwork about 690 px wide; a full-size illustration is
    several megabytes and would dominate the rendered document.

    Args:
        payload: Original PNG or JPEG bytes.
        size: Length of the longer edge after downscaling.

    Returns:
        A ``data:`` URI, or an empty string when the image cannot be decoded.
    """
    try:
        from PIL import Image  # Shipped with AstrBot.

        with Image.open(io.BytesIO(payload)) as source:
            source.thumbnail((size, size), Image.LANCZOS)
            flattened = Image.new("RGB", source.size, (255, 255, 255))
            if source.mode in {"RGBA", "LA", "P"}:
                converted = source.convert("RGBA")
                flattened.paste(converted, mask=converted.split()[-1])
            else:
                flattened.paste(source.convert("RGB"))
            buffer = io.BytesIO()
            flattened.save(buffer, format="JPEG", quality=88, optimize=True)
    except (ImportError, OSError, ValueError):
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode(
        "ascii"
    )


def _text(value: Any) -> str:
    """Escape a value unless it already carries card markup.

    Args:
        value: Plain text, or HTML produced by a provider's description
            renderer.

    Returns:
        HTML-safe text.
    """
    if isinstance(value, SafeHtml):
        return str(value)
    return escape(str(value))


def _icon(url: str, extra: str = "") -> str:
    """Render an ``<img>`` tag for an optional icon.

    Args:
        url: Icon URL; an empty string renders nothing.
        extra: Additional inline style declarations.

    Returns:
        The image markup, or an empty string.
    """
    if not url:
        return ""
    style = f' style="{extra}"' if extra else ""
    return f'<img src="{escape(url, quote=True)}"{style} alt="">'


def _blocks(grid: list[list[str]]) -> str:
    """Render a range grid as the template's block rows.

    Args:
        grid: Rows of cell kinds returned by a provider.

    Returns:
        One ``.block-group`` per row; empty when the grid is empty.
    """
    rows = []
    for row in grid:
        cells = "".join(
            '<div class="block s"></div>'
            if cell == "s"
            else '<div class="block r"></div>'
            if cell == "r"
            else '<div class="block"></div>'
            for cell in row
        )
        rows.append(f'<div class="block-group">{cells}</div>')
    return "".join(rows)


def _attr_cells(rows: list[dict[str, str]]) -> str:
    """Render attribute or affiliation cells.

    Args:
        rows: Entries carrying ``label``, ``value`` and an optional ``bonus``.

    Returns:
        The ``.attr`` cells of the three-column table.
    """
    cells = []
    for row in rows:
        bonus = (
            f'<span class="a">+{escape(row["bonus"])}</span>'
            if row.get("bonus")
            else ""
        )
        cells.append(
            f'<div class="attr" title="{escape(row["label"])}">'
            f"<div>{escape(row['value'])}{bonus}</div></div>"
        )
    return "".join(cells)


def _section(title: str, body: str) -> str:
    """Wrap one right-column section.

    Args:
        title: Section heading.
        body: Pre-rendered rows.

    Returns:
        The section markup; empty when ``body`` is empty.
    """
    if not body:
        return ""
    return f'<div><div class="title">{escape(title)}</div>{body}</div>'


def _render_profile(
    card: dict[str, Any],
    font: str | None,
    potential_block: str,
    real_name_block: str,
    cv_block: str,
) -> str:
    """Render the profile layout used by games without skills or talents.

    Args:
        card: Mapping produced by a provider's ``card`` method.
        font: Font file name the document may reference relatively.
        potential_block: Pre-rendered potential section.
        real_name_block: Pre-rendered real-name section.
        cv_block: Pre-rendered voice-actor section.

    Returns:
        A complete HTML document: values and archive rows on the left, the
        full illustration on the right.
    """
    stars = "★" * int(card.get("rarity") or 0)
    art = card.get("art") or card.get("portrait") or ""
    art_html = (
        f'<img src="{escape(art, quote=True)}" alt="" '
        'style="width:100%;display:block;border-radius:6px">'
        if art
        else ""
    )
    class_line = escape(card.get("class_name") or "")
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        f"<style>{_style(font)}</style>"
        "<style>"
        "html, body { margin: 0; }"
        "#template { width: 1400px; min-height: 900px; }"
        ".background { min-height: 900px; }"
        ".profile { display: flex; gap: 24px; padding: 18px; }"
        ".profile-left { width: 800px; flex: none; }"
        ".profile-right { width: calc(100% - 824px); flex: none; }"
        ".profile .operator-attr { display: flex; flex-wrap: wrap; width: 100%; }"
        ".profile .attr { width: 33.333%; }"
        "</style></head><body>"
        '<div id="template"><div class="background">'
        '<div class="profile">'
        '<div class="profile-left">'
        '<div class="operator-attr">'
        f"{_attr_cells(card.get('attributes') or [])}"
        f"{_attr_cells(card.get('affiliation') or [])}</div>"
        f"{potential_block}{real_name_block}{cv_block}"
        '<div style="padding-top:18px">'
        f'<div class="name" style="font-size:52px">{escape(card["name"])}</div>'
        f'<div class="name" style="font-size:30px">{escape(card.get("en_name") or "")}</div>'
        f'<div style="font-size:30px">{stars}'
        f'<span style="margin-left:12px;font-size:24px">{class_line}</span></div>'
        "</div></div>"
        f'<div class="profile-right">{art_html}</div>'
        "</div></div></div></body></html>"
    )


def render_card(card: dict[str, Any], *, font: str | None = None) -> str:
    """Build the HTML document of one character card.

    Args:
        card: Mapping produced by a provider's ``card`` method.
        font: Font file name the document may reference relatively, matching
            the template's ``font.css``; ``None`` keeps the fallback stack that
            the remote render service resolves with its own fonts.

    Returns:
        A complete HTML document that the render service turns into a PNG.
    """
    stars = "".join("<i>★</i>" for _ in range(int(card.get("rarity") or 0)))
    class_line = " - ".join(
        part
        for part in (card.get("class_name") or "", card.get("sub_class") or "")
        if part
    )
    class_icon = card.get("class_icon") or ""

    potential_rows = []
    for item in card.get("potential") or []:
        rank = item.get("rank")
        # The template draws the badge that belongs to the rank, not the
        # in-game potential icon, so the black plate is part of the asset.
        icon = _rank_icon(int(rank)) if isinstance(rank, int) else ""
        potential_rows.append(
            '<div class="potential">'
            + _icon(icon or item.get("icon") or "", "width:25px;margin-right:10px")
            + _text(item["text"])
            + "</div>"
        )
    potential = "".join(potential_rows)
    if card.get("cv"):
        cv_rows = (
            "<table>"
            + "".join(
                f"<tr><td>{escape(row['lang'])}：</td>"
                f"<td>{escape(row['names'])}</td></tr>"
                for row in card.get("cv") or []
            )
            + "</table>"
        )
    else:
        cv_rows = ""

    potential_block = (
        f'<div><div class="title" style="width:120px">潜能提升</div>{potential}</div>'
        if potential
        else ""
    )
    real_name_block = (
        '<div><div class="title" style="width:120px;margin-top:5px">真名</div>'
        f"<div>{escape(card.get('real_name') or '未知')}</div></div>"
    )
    cv_block = (
        '<div><div class="title" style="width:120px;margin-top:5px">CV</div>'
        f"{cv_rows or '<div>未知</div>'}</div>"
    )

    # Games without skills, talents or devices leave the right column empty;
    # the potential and voice blocks move there so the card stays balanced.
    right_has_content = bool(
        card.get("impression")
        or card.get("token")
        or card.get("talents")
        or card.get("base_skills")
        or card.get("skills")
        or card.get("devices")
    )
    left_extra = f"{potential_block}{real_name_block}{cv_block}"
    left = (
        '<div><div class="operator-attr">'
        f"{_attr_cells(card.get('attributes') or [])}"
        f"{_attr_cells(card.get('affiliation') or [])}</div>"
        f"{left_extra}</div>"
        '<div class="operator-name">'
        f'<div class="range" style="margin:15px 0">{_blocks(card.get("range") or [])}</div>'
        '<div class="class-info">'
        + (
            '<div style="width:60px;padding:5px;background:#000">'
            f"{_icon(class_icon, 'width:100%')}</div>"
            if class_icon
            else ""
        )
        + f'<div class="rarity"><div style="font-size:30px;display:flex">{stars}</div>'
        f"<div>{escape(class_line)}</div></div></div>"
        '<div class="tags">'
        + "".join(
            f'<span style="background:#9c27b0">{escape(tag)}</span>'
            if tag == "异格干员"
            else f"<span>{escape(tag)}</span>"
            for tag in card.get("tags") or []
        )
        + "</div>"
        f'<div class="trait">{_text(card.get("trait") or "")}</div>'
        f'<div class="name" style="padding-top:10px;font-size:50px">'
        f"{escape(card['name'])}</div>"
        '<div class="name" style="padding-bottom:10px;font-size:30px">'
        + (f"<span>{escape(card['number'])}</span>" if card.get("number") else "")
        + f"{escape(card.get('en_name') or '')}</div></div>"
    )

    impression = ""
    for index, line in enumerate(card.get("impression") or []):
        text = f"<i>{escape(line)}</i>" if index else escape(line)
        impression += f'<div class="exInfo">{text}</div>'

    token = card.get("token")
    token_html = (
        '<div class="exInfo"><div style="width:100%">'
        f"{escape(token.get('name') or '')}<br>{escape(token['text'])}</div></div>"
        if token
        else ""
    )

    talents = "".join(
        f'<div class="exInfo"><div style="width:120px">{escape(item["name"])}</div>'
        '<div style="width:calc(100% - 120px);padding-left:10px">'
        f"{_text(item['text'])}</div></div>"
        for item in card.get("talents") or []
    )

    base_skills = "".join(
        f'<div class="exInfo bs"><div style="width:60px">精英{escape(item["elite"])}</div>'
        f'<div class="bs-icon" style="background-image:url(\'{escape(item["icon"])}\')"></div>'
        f'<div style="width:100px">{escape(item["name"])}</div>'
        '<div style="width:calc(100% - 160px);padding-left:10px">'
        f"{_text(item['text'])}</div></div>"
        for item in card.get("base_skills") or []
    )

    skills = ""
    for item in card.get("skills") or []:
        chips = ""
        if item.get("sp_type_name"):
            chips += (
                f'<div class="t{escape(item["sp_type"])}">'
                f"{escape(item['sp_type_name'])}</div>"
            )
        if item.get("skill_type_name"):
            chips += f"<div>{escape(item['skill_type_name'])}</div>"
        if item.get("sp_cost"):
            chips += (
                f"<div>技力 {escape(item['sp_init'])}/{escape(item['sp_cost'])}</div>"
            )
        if float(item.get("duration") or 0) > 0:
            chips += f"<div>持续 {escape(item['duration'])} 秒</div>"
        skills += (
            '<div class="skill-item">'
            '<div style="width:120px;text-align:center">'
            f"<div>{_icon(item.get('icon') or '', 'width:80px')}</div>"
            f"<div>{escape(item['name'])}</div></div>"
            '<div style="width:calc(100% - 240px);padding:0 10px">'
            f'<div class="skill-type" style="margin-bottom:5px">{chips}</div>'
            f'<div class="skill-desc">{_text(item["text"])}</div></div>'
            '<div class="skill-range"><div style="display:flex;flex-direction:column">'
            f"{_blocks(item.get('range') or [])}</div></div>"
            "</div>"
        )

    devices = ""
    for device in card.get("devices") or []:
        devices += (
            '<div class="exInfo"><div style="width:100%">'
            f"<b>{escape(device['name'])}</b><br>{escape(device.get('trait') or '')}"
            "</div></div>"
        )
        devices += (
            '<div class="exInfo">'
            + " ".join(
                f"{escape(row['label'])} {escape(row['value'])}"
                for row in device.get("attributes") or []
            )
            + "</div>"
        )
        for skill in device.get("skills") or []:
            devices += (
                '<div class="exInfo"><div style="width:100%">'
                f"<b>{escape(skill['name'])}</b> {escape(skill.get('meta') or '')}"
                f"<br>{_text(skill.get('text') or '')}</div></div>"
            )

    right = "".join(
        (
            _section("干员印象", impression),
            _section("信物", token_html),
            _section("天赋", talents),
            _section("基建技能", base_skills),
            _section("装置", devices),
        )
    )
    if skills:
        right += f'<div class="skills">{skills}</div>'

    if not right_has_content:
        # The profile layout belongs to the Endfield card, which has no real
        # name and may publish no voice actors, so empty blocks are dropped.
        return _render_profile(
            card,
            font,
            potential_block,
            real_name_block if card.get("real_name") else "",
            cv_block if card.get("cv") else "",
        )

    art = card.get("art") or card.get("portrait") or ""
    background_style = f"background-image:url('{escape(art)}')" if art else ""
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        f"<style>{_style(font)}</style></head><body>"
        f'<div id="template"><div class="background" style="{background_style}">'
        f'<div class="left">{left}</div><div class="right">{right}</div>'
        "</div></div></body></html>"
    )


_RECRUIT_STYLE = """
* { font-size: 18px; box-sizing: border-box; }
body { margin: 0; }
#template {
    width: 1280px; min-height: 720px; padding: 20px; color: #fff;
    background: url(BACKGROUND_URL) center / cover no-repeat;
    overflow: hidden; position: relative;
}
.group-list { display: flex; flex-wrap: wrap; }
.group-list > div { margin: 0 30px 20px 0; padding-left: 10px; border-left: 5px solid #424242; }
span.tag { padding: 2px 5px; margin-right: 5px; background: #343434; }
.operator-list { margin-top: 10px; display: flex; flex-wrap: wrap; }
.operator { width: 60px; display: flex; flex-direction: column; }
.operator > img { width: 100%; border-bottom: 5px solid gray; }
.operator > span { margin-top: 2px; font-size: 12px; }
.rarity6 { border-color: #f57c00 !important; }
.rarity5 { border-color: #ffc107 !important; }
.rarity4 { border-color: #d1c4e9 !important; }
"""


def _recruit_style(font: str | None = None) -> str:
    """Build the recruit stylesheet with the shared background texture.

    Args:
        font: Optional font family name injected by the local renderer.

    Returns:
        The complete stylesheet.
    """
    style = _RECRUIT_STYLE.replace("BACKGROUND_URL", _token_background())
    if font:
        style = (
            f"@font-face {{ font-family: 'Harmony'; src: url('{font}') format('truetype'); }}\n"
            "* { font-family: 'Harmony', serif !important; }\n" + style
        )
    return style


def render_recruit(page: dict[str, Any], *, font: str | None = None) -> str:
    """Render the recruitment result page.

    The markup mirrors ``template/operatorRecruit.html`` of the reference
    plugin: one column per tag combination, tag pills on top, the operators
    below, and no title or truncation.

    Args:
        page: Mapping from ``ArknightsProvider.recruit``.
        font: Optional font family name injected by the local renderer.

    Returns:
        A complete HTML document.
    """
    groups = "".join(
        "<div><div>"
        + "".join(
            f'<span class="tag">{escape(str(tag))}</span>' for tag in group["tags"]
        )
        + '</div><div class="operator-list">'
        + "".join(
            f'<div class="operator"><img class="rarity{int(item["rarity"])}" '
            f'src="{escape(item["avatar"])}" alt="{escape(item["name"])}">'
            f"<span>{escape(item['name'])}</span></div>"
            for item in group["operators"]
        )
        + "</div></div>"
        for group in page.get("groups") or []
    )
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        f"<style>{_recruit_style(font)}</style></head><body>"
        f'<div id="template"><div class="group-list">{groups}</div></div>'
        "</body></html>"
    )


_PANEL_STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Harmony', 'Noto Sans CJK SC', sans-serif; }
body { margin: 0; background: #101418; }
#panel { width: 1280px; min-height: 720px; padding: 28px 32px; color: #f5f5f5;
         background: url(TOKEN_BACKGROUND_URL) center / cover no-repeat; }
.head { display: flex; align-items: center; gap: 18px; margin-bottom: 22px; }
.head img { width: 76px; height: 76px; border-radius: 50%; object-fit: cover;
            border: 3px solid #f5f5f5; background: #2a2f36; }
.head .title { font-size: 40px; font-weight: bold; letter-spacing: 1px; }
.head .subtitle { font-size: 20px; color: #b9c2cc; margin-top: 4px; }
.sec { margin-bottom: 22px; padding: 16px 18px; background: rgb(20 24 30 / 78%);
       border-left: 6px solid #00b3ff; border-radius: 4px; }
.sec-title { font-size: 24px; font-weight: bold; color: #fff; margin-bottom: 10px; }
.sec-meta { font-size: 16px; color: #9fb0c0; margin-bottom: 8px; }
.row { display: flex; font-size: 18px; line-height: 1.8; }
.row .k { width: 130px; color: #9fb0c0; flex: none; }
.row .v { flex: 1; word-break: break-all; }
.text { font-size: 18px; line-height: 1.75; color: #e6ebf0; white-space: pre-wrap; }
.grid { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; }
.grid figure { width: 200px; }
.grid img { width: 200px; display: block; border-radius: 4px; background: #1b2027; }
.grid figcaption { font-size: 14px; color: #c8d2dc; margin-top: 6px; text-align: center; }
"""


def render_panel(panel: dict[str, Any], *, font: str | None = None) -> str:
    """Render a dark info page shared by the operator sub-queries.

    Args:
        panel: Mapping with ``title``, ``subtitle``, ``avatar`` and ``sections``;
            each section holds ``title``, optional ``meta``/``text``, ``rows``
            of ``(label, value)`` and an optional ``images`` list of
            ``{"src", "caption"}``.
        font: Optional ``@font-face`` source for the bundled HarmonyOS font.

    Returns:
        The HTML document for the local renderer.
    """
    style = _PANEL_STYLE.replace("TOKEN_BACKGROUND_URL", _token_background())
    if font:
        style = (
            "@font-face { font-family: 'Harmony'; src: url('"
            + font
            + "') format('truetype'); }\n"
            + style
        )

    blocks = []
    avatar = panel.get("avatar") or ""
    head = ""
    if avatar:
        head += f'<img src="{avatar}" alt="">'
    head += (
        f'<div><div class="title">{escape(str(panel.get("title") or ""))}</div>'
        f'<div class="subtitle">{escape(str(panel.get("subtitle") or ""))}</div></div>'
    )
    blocks.append(f'<div class="head">{head}</div>')

    for section in panel.get("sections") or []:
        inner = [
            f'<div class="sec-title">{escape(str(section.get("title") or ""))}</div>'
        ]
        meta = section.get("meta")
        if meta:
            inner.append(f'<div class="sec-meta">{escape(str(meta))}</div>')
        for label, value in section.get("rows") or []:
            inner.append(
                f'<div class="row"><div class="k">{escape(str(label))}</div>'
                f'<div class="v">{_text(value)}</div></div>'
            )
        text = section.get("text")
        if text:
            inner.append(f'<div class="text">{_text(text)}</div>')
        images = section.get("images") or []
        if images:
            figures = "".join(
                f'<figure><img src="{escape(str(item["src"]))}" alt="">'
                f"<figcaption>{escape(str(item.get('caption') or ''))}</figcaption></figure>"
                for item in images
            )
            inner.append(f'<div class="grid">{figures}</div>')
        blocks.append(f'<div class="sec">{"".join(inner)}</div>')

    return (
        '<html><head><meta charset="utf-8">'
        f"<style>{style}</style></head><body>"
        f'<div id="panel">{"".join(blocks)}</div></body></html>'
    )
