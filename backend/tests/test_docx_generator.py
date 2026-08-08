"""
Unit tests for docx generation service.
"""
from backend.services.docx_generator import _parse_formatted_runs, render_docx


def test_parse_formatted_runs_plain_text():
    runs = _parse_formatted_runs("Обычный текст без выделений")
    assert len(runs) == 1
    assert runs[0] == ("Обычный текст без выделений", False)


def test_parse_formatted_runs_bold_text():
    runs = _parse_formatted_runs("Текст с **жирным** выделением")
    assert len(runs) == 3
    assert runs[0] == ("Текст с ", False)
    assert runs[1] == ("жирным", True)
    assert runs[2] == (" выделением", False)


def test_render_docx_generates_valid_bytes():
    sample_protocol = (
        "ПРОТОКОЛ\n"
        "судебного заседания\n\n"
        "г. Нижневартовск\t12 мая 2026 года\n"
        "Председательствующий: Объявляется судебное заседание.\n"
        "Подсудимый: Права понятны.\n\n"
        "Председательствующий\tИванов И.И.\n"
        "Секретарь\tПетрова П.П.\n"
    )
    docx_bytes = render_docx(sample_protocol)
    assert isinstance(docx_bytes, bytes)
    assert len(docx_bytes) > 0
    # DOCX zip magic header PK\x03\x04
    assert docx_bytes.startswith(b"PK\x03\x04")
