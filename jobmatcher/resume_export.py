"""Assemble a complete source snapshot; never ask AI to invent resume sections."""
import re
from pathlib import Path
from fpdf import FPDF


def assemble(text, bullets):
    replacements = []
    for bullet in bullets:
        original, proposed = bullet["original"].strip(), bullet["proposed"].strip()
        pattern = r"\s+".join(re.escape(word) for word in original.split())
        matches = list(re.finditer(pattern, text))
        if len(matches) != 1:
            raise ValueError("A source bullet changed or is ambiguous; regenerate the draft.")
        match = matches[0]
        if any(match.start() < end and match.end() > start for start, end, _ in replacements):
            raise ValueError("Duplicate or overlapping source bullets; regenerate the draft.")
        replacements.append((match.start(), match.end(), proposed))
    for start, end, proposed in sorted(replacements, reverse=True):
        text = text[:start] + proposed + text[end:]
    return text


def faithful_wording(original, proposed):
    # New content words can introduce tools, duties or claims even without new numbers.
    glue = set("a an the and or of to in on for with by from as at is are was were be been being that this these those using used use".split())
    words = lambda value: {word.strip('.-/') for word in re.findall(r"[\w+#.%/-]+", value.lower())} - glue
    return bool(proposed.strip()) and words(proposed).issubset(words(original))


# Export layout: Letter, 36pt side margins, Times-style
# serif, 18pt name, 11pt ruled headings, 8.5pt body with 10pt leading.
FORMAT_VERSION = 'networking-v1'
SECTIONS = {
    'summary', 'professional summary', 'profile', 'technical skills', 'skills',
    'education', 'experience', 'professional experience', 'work experience',
    'projects', 'certificates', 'certifications', 'additional training',
    'training & activities', 'extracurricular activities', 'training',
}


def _blocks(text):
    blocks, section = [], ''
    text = re.sub(r'[ \t]{3,}(?=[•▪◦])', '\n', text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not blocks:
            blocks.append(('name', line, ''))
            continue
        heading = line.lower() in SECTIONS or bool(re.fullmatch(r'[A-Z][A-Z &/]{2,55}', line))
        bullet = bool(re.match(r'^[•▪◦]\s*|^-\s+', line))
        if heading and not bullet:
            section = line.lower()
            blocks.append(('section', line.upper(), ''))
        elif not section:
            blocks.append(('contact', re.sub(r'\s+', ' ', line), ''))
        elif bullet:
            blocks.append(('bullet', re.sub(r'\s+', ' ', line), ''))
        elif blocks[-1][0] == 'bullet' and raw[:1].isspace():
            kind, previous, right = blocks[-1]
            blocks[-1] = (kind, previous + ' ' + re.sub(r'\s+', ' ', line), right)
        elif section in ('summary', 'professional summary', 'profile'):
            if blocks[-1][0] == 'paragraph':
                blocks[-1] = ('paragraph', blocks[-1][1] + ' ' + line, '')
            else:
                blocks.append(('paragraph', line, ''))
        else:
            columns = re.split(r'[ \t]{3,}', line, maxsplit=1)
            kind = 'entry'
            if section in ('education', 'experience', 'professional experience', 'work experience') and blocks[-1][0] == 'entry' and not (len(columns) == 2 and re.search(r'\b(?:19|20)\d{2}\b', columns[1])):
                kind = 'detail'
            blocks.append((kind, columns[0], columns[1] if len(columns) == 2 else ''))
    return blocks


def pdf_bytes(text):
    directory = next((path for path in (
        Path('/usr/share/fonts/truetype/liberation'),
        Path('/usr/share/fonts/truetype/liberation2'),
    ) if (path / 'LiberationSerif-Regular.ttf').exists()), None)
    if directory is None:
        raise RuntimeError('Liberation Serif system fonts are required for the networking resume format.')
    if not text.strip():
        raise ValueError('Cannot export an empty resume.')
    pdf = FPDF(unit='pt', format='letter')
    pdf.set_margins(36, 24, 36)
    pdf.set_auto_page_break(True, 24)
    pdf.c_margin = 0
    pdf.set_title('Resume')
    pdf.set_creator('Job Matcher - Networking resume format')
    for style, suffix in [('', 'Regular'), ('B', 'Bold'), ('I', 'Italic')]:
        pdf.add_font('Resume', style=style, fname=str(directory / f'LiberationSerif-{suffix}.ttf'))
    pdf.add_page()
    section = ''

    def room(height):
        if pdf.will_page_break(height):
            pdf.add_page()

    def flow(value, size=8.5, style='', indent=0, markdown=False, align='L'):
        pdf.set_font('Resume', style=style, size=size)
        pdf.set_x(36 + indent)
        pdf.multi_cell(540 - indent, 10, value, align=align, markdown=markdown,
                       new_x='LMARGIN', new_y='NEXT')

    previous_kind = None
    blocks = _blocks(text)
    for index, (kind, left, right) in enumerate(blocks):
        next_kind = blocks[index + 1][0] if index + 1 < len(blocks) else None
        # Keep source punctuation while normalizing typography-only Unicode dashes.
        left = left.translate(str.maketrans({'–': '-', '—': '-', '‑': '-'}))
        right = right.translate(str.maketrans({'–': '-', '—': '-', '‑': '-'}))
        if kind == 'name':
            pdf.set_font('Resume', size=18)
            # Wrap an unusually long name rather than reducing every resume's type.
            pdf.multi_cell(540, 22, left, align='C', new_x='LMARGIN', new_y='NEXT')
        elif kind == 'contact':
            flow(left, size=7.5, align='C')
        elif kind == 'section':
            room(40)
            pdf.ln(6)
            pdf.set_font('Resume', style='B', size=11)
            pdf.cell(540, 13, left, new_x='LMARGIN', new_y='NEXT')
            pdf.set_line_width(0.4)
            pdf.line(36, pdf.get_y(), 576, pdf.get_y())
            pdf.ln(3)
            section = left.lower()
        elif kind in ('entry', 'detail'):
            keep = 30 if next_kind == 'detail' else 20 if next_kind in ('bullet', 'paragraph') else 10
            if kind == 'entry' and previous_kind in ('bullet', 'detail'):
                room(keep + 4)
                pdf.ln(4)
            room(keep)
            pdf.set_font('Resume', style='B' if kind == 'entry' else 'I', size=9)
            if right and pdf.get_string_width(left) + pdf.get_string_width(right) + 12 <= 540:
                y = pdf.get_y()
                pdf.cell(540 - pdf.get_string_width(right) - 12, 10, left)
                pdf.set_xy(576 - pdf.get_string_width(right), y)
                pdf.cell(pdf.get_string_width(right), 10, right, new_x='LMARGIN', new_y='NEXT')
            else:
                flow(left, size=9, style='B' if kind == 'entry' else 'I')
                if right:
                    flow(right, size=9, style='B' if kind == 'entry' else 'I', align='R')
        elif kind == 'bullet':
            room(10)
            pdf.set_font('Resume', size=8.5)
            pdf.cell(5.5, 10, left[0])
            body = re.sub(r'^[•▪◦]\s*|^-\s+', '', left)
            emphasize = (section in ('technical skills', 'skills') or body.startswith('GPA:')) and ':' in body and not any(mark in body for mark in ('*', '__', '~~'))
            if emphasize:
                label, rest = body.split(':', 1)
                body = '**' + label + ':**' + rest
            flow(body, indent=5.5, markdown=emphasize)
        else:
            flow(left)
        previous_kind = kind
    return bytes(pdf.output())
